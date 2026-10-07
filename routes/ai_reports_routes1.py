"""
AI Reports Routes - Natural language to SQL reporting
"""
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
from datetime import datetime
import time
import os
import asyncio

from database import get_db
from models import User, AIQueryHistory
from auth import get_current_active_user
from ai_services.ai_factory import ai_factory
from ai_services.schema_manager import get_database_schema
from ai_services.sql_validator import SQLValidator
from ai_services.query_optimizer import QueryOptimizer
from ai_services.chart_generator import chart_generator
from ai_services.ai_logger import timed_step, log_request_start, log_request_complete, log_request_failed, check_llm_threshold, check_db_threshold, logger as ai_logger
import uuid

router = APIRouter(prefix="/ai-reports", tags=["AI Reports"])

# Initialize services
sql_validator = SQLValidator()
query_optimizer = QueryOptimizer()

# ── Request Queue (Semaphore) ─────────────────────────────────────────────────
# Limits concurrent LLM calls to 1 at a time — prevents OOM crashes on CPU Ollama.
# Configurable via .env: AI_MAX_CONCURRENT (default 1), AI_QUEUE_TIMEOUT_SEC (default 120)
_llm_semaphore = asyncio.Semaphore(
    int(os.getenv("AI_MAX_CONCURRENT", "1"))
)
_QUEUE_TIMEOUT = int(os.getenv("AI_QUEUE_TIMEOUT_SEC", "120"))  # max seconds to wait in queue

@router.post("/ask")
async def ask_ai_report(
    question: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Ask a question in natural language and get SQL results
    """
    # Check if AI is enabled
    if not os.getenv("AI_ENABLED", "true").lower() == "true":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI reporting is currently disabled"
        )
    
    # Check user role
    if current_user.role not in ["district", "block", "sub_centre"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI reporting is only available for district, block, and sub-centre users"
        )
    
    start_time = time.time()
    request_id = str(uuid.uuid4())[:8]
    timing_breakdown = {}
    failed_step = None
    
    log_request_start(request_id, current_user.id, current_user.role, len(question))
    
    try:
        # Stage 1: Get AI engine
        with timed_step(request_id, "ENGINE_INIT") as t:
            ai_engine = ai_factory.get_engine()
        timing_breakdown["engine_init_ms"] = t["ms"]
        
        # Stage 2: Get database schema
        with timed_step(request_id, "SCHEMA_LOAD") as t:
            schema = get_database_schema()
        timing_breakdown["schema_load_ms"] = t["ms"]
        
        # Stage 3: Prepare user context
        with timed_step(request_id, "CONTEXT_BUILD") as t:
            user_context = {
                "district_id": current_user.district_id,
                "block_id": current_user.block_id,
                "sub_centre_id": current_user.sub_centre_id
            }
        timing_breakdown["context_build_ms"] = t["ms"]
        
        # Stage 4: Generate SQL using AI (THE BIG ONE)
        # Acquire semaphore — ensures only 1 LLM call runs at a time on CPU Ollama
        ai_logger.info(
            f"[AI-REPORT] [request_id={request_id}] [step=QUEUE_WAIT] [status=START] "
            f"[queue_depth={_llm_semaphore._value}] [timeout_sec={_QUEUE_TIMEOUT}]"
        )
        try:
            await asyncio.wait_for(_llm_semaphore.acquire(), timeout=_QUEUE_TIMEOUT)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "message": "AI service is busy. Too many requests queued. Please try again in a moment.",
                    "failed_step": "QUEUE_WAIT",
                    "timing": timing_breakdown
                }
            )
        ai_logger.info(
            f"[AI-REPORT] [request_id={request_id}] [step=QUEUE_WAIT] [status=ACQUIRED]"
        )
        try:
            with timed_step(request_id, "LLM_CALL", f"[provider={ai_engine.name}]") as t:
                generated_sql = ai_engine.generate_sql(
                    question=question,
                    schema=schema,
                    user_role=current_user.role,
                    user_context=user_context
                )
        finally:
            _llm_semaphore.release()
            ai_logger.info(
                f"[AI-REPORT] [request_id={request_id}] [step=QUEUE_WAIT] [status=RELEASED]"
            )
        timing_breakdown["llm_call_ms"] = t["ms"]
        check_llm_threshold(request_id, t["ms"])
        
        # Stage 5: Clean SQL
        with timed_step(request_id, "SQL_CLEAN") as t:
            generated_sql = sql_validator.clean_sql(generated_sql)
        timing_breakdown["sql_clean_ms"] = t["ms"]
        
        # Stage 6: Validate SQL
        with timed_step(request_id, "SQL_VALIDATE") as t:
            is_valid, error_msg = sql_validator.validate(generated_sql)
        timing_breakdown["sql_validate_ms"] = t["ms"]
        
        if not is_valid:
            failed_step = "SQL_VALIDATE"
            execution_time = int((time.time() - start_time) * 1000)
            log_request_failed(request_id, execution_time, failed_step, error_msg)
            return {
                "success": False,
                "message": f"Invalid SQL: {error_msg}",
                "question": question,
                "generated_sql": generated_sql,
                "error": error_msg,
                "execution_time_ms": execution_time,
                "timing": timing_breakdown
            }
        
        # Stage 7: Add role-based filters
        with timed_step(request_id, "ROLE_FILTER") as t:
            optimized_sql = query_optimizer.add_role_filters(
                generated_sql,
                current_user.role,
                user_context
            )
        timing_breakdown["role_filter_ms"] = t["ms"]
        
        # Stage 8: DB connection ping
        with timed_step(request_id, "DB_PING") as t:
            try:
                db.execute(text("SELECT 1"))
            except:
                try:
                    db.rollback()
                except:
                    pass
        timing_breakdown["db_ping_ms"] = t["ms"]
        
        # Stage 9: Execute query
        with timed_step(request_id, "DB_EXECUTE", f"[sql_chars={len(optimized_sql)}]") as t:
            try:
                result = db.execute(text(optimized_sql))
                rows = result.fetchall()
                columns = result.keys()
                data = [dict(zip(columns, row)) for row in rows]
            except Exception as db_error:
                db.rollback()
                raise db_error
        timing_breakdown["db_execute_ms"] = t["ms"]
        check_db_threshold(request_id, t["ms"])
        
        # Stage 10: Generate chart config
        with timed_step(request_id, "CHART_GEN") as t:
            visualization = chart_generator.generate_chart_config(data, optimized_sql, question)
        timing_breakdown["chart_gen_ms"] = t["ms"]
        
        # Calculate total execution time
        execution_time = int((time.time() - start_time) * 1000)
        timing_breakdown["total_ms"] = execution_time
        
        # Stage 11: Save to history
        import json
        with timed_step(request_id, "HISTORY_SAVE") as t:
            try:
                history = AIQueryHistory(
                    user_id=current_user.id,
                    user_role=current_user.role,
                    natural_query=question,
                    generated_sql=optimized_sql,
                    execution_time_ms=execution_time,
                    result_count=len(data),
                    success=True,
                    ai_provider=ai_engine.name,
                    response_data=json.dumps(data, default=str),
                    visualization_config=json.dumps(visualization, default=str) if visualization else None
                )
                db.add(history)
                db.commit()
                db.refresh(history)
            except Exception as commit_error:
                db.rollback()
                history = type('obj', (object,), {'id': 0})()
        timing_breakdown["history_save_ms"] = t["ms"]
        
        log_request_complete(request_id, execution_time, len(data), ai_engine.name)
        
        return {
            "success": True,
            "query_id": history.id,
            "question": question,
            "generated_sql": generated_sql,
            "optimized_sql": optimized_sql,
            "data": data,
            "result_count": len(data),
            "execution_time_ms": execution_time,
            "ai_provider": ai_engine.name,
            "visualization": visualization,
            "timing": timing_breakdown
        }
        
    except HTTPException:
        raise
    except Exception as e:
        try:
            db.rollback()
        except:
            pass
        
        execution_time = int((time.time() - start_time) * 1000)
        timing_breakdown["total_ms"] = execution_time
        log_request_failed(request_id, execution_time, failed_step or "UNKNOWN", f"{type(e).__name__}: {e}")
        
        try:
            history = AIQueryHistory(
                user_id=current_user.id,
                user_role=current_user.role,
                natural_query=question,
                generated_sql=None,
                execution_time_ms=execution_time,
                result_count=0,
                success=False,
                error_message=str(e),
                ai_provider=ai_factory.get_engine().name if ai_factory._current_engine else "unknown"
            )
            db.add(history)
            db.commit()
        except:
            pass
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "message": f"Error processing query: {str(e)}",
                "failed_step": failed_step or "UNKNOWN",
                "timing": timing_breakdown
            }
        )

@router.get("/history")
async def get_query_history(
    skip: int = 0,
    limit: int = 50,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get user's query history with full response data and optional date filters"""
    import json
    from datetime import datetime
    
    query = db.query(AIQueryHistory).filter(
        AIQueryHistory.user_id == current_user.id
    )
    
    # Apply date filters if provided
    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            query = query.filter(AIQueryHistory.created_at >= start_dt)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid start_date format. Use ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"
            )
    
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            query = query.filter(AIQueryHistory.created_at <= end_dt)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid end_date format. Use ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"
            )
    
    history_records = query.order_by(AIQueryHistory.created_at.desc()).offset(skip).limit(limit).all()
    
    # Format response with parsed JSON
    formatted_history = []
    for record in history_records:
        formatted_record = {
            "id": record.id,
            "question": record.natural_query,
            "success": record.success,
            "result_count": record.result_count,
            "execution_time_ms": record.execution_time_ms,
            "ai_provider": record.ai_provider,
            "created_at": record.created_at,
            "data": json.loads(record.response_data) if record.response_data else None,
            "visualization": json.loads(record.visualization_config) if record.visualization_config else None,
            "generated_sql": record.generated_sql,
            "error_message": record.error_message
        }
        formatted_history.append(formatted_record)
    
    return {
        "success": True,
        "history": formatted_history,
        "total": len(formatted_history),
        "filters": {
            "start_date": start_date,
            "end_date": end_date
        }
    }

@router.get("/engines")
async def get_available_engines(
    current_user: User = Depends(get_current_active_user)
):
    """Get list of available AI engines"""
    
    if current_user.role not in ["district", "block"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    current_provider = os.getenv("AI_PROVIDER", "gemini")
    available = ai_factory.get_available_engines()
    
    return {
        "success": True,
        "current_provider": current_provider,
        "available_engines": available
    }

@router.post("/reload-engine")
async def reload_ai_engine(
    current_user: User = Depends(get_current_active_user)
):
    """Reload AI engine after .env changes"""
    
    if current_user.role != "district":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only district users can reload engine"
        )
    
    try:
        ai_factory.reload_engine()
        return {
            "success": True,
            "message": "AI engine reloaded successfully",
            "current_provider": os.getenv("AI_PROVIDER")
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error reloading engine: {str(e)}"
        )
