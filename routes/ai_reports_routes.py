"""
AI Reports Routes - Natural language to SQL reporting
Async job system: POST /ask returns job_id instantly,
background task calls Ollama, GET /status/{job_id} returns result when ready.
"""
from fastapi import APIRouter, Depends, HTTPException, status, Request, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
from datetime import datetime
import time
import os
import asyncio
import json

from database import get_db, SessionLocal
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

# Limits concurrent LLM calls to 1 at a time — prevents OOM on CPU Ollama
_llm_semaphore = asyncio.Semaphore(int(os.getenv("AI_MAX_CONCURRENT", "1")))


def _run_ai_job(job_id: int, question: str, user_id: int, user_role: str, user_context: dict):
    """
    Background task: calls Ollama → runs SQL → saves result to DB.
    Uses its own DB session (background tasks can't share the request session).
    No timeout pressure — runs as long as needed.
    """
    db = SessionLocal()
    request_id = str(uuid.uuid4())[:8]
    start_time = time.time()

    try:
        ai_engine = ai_factory.get_engine()
        schema = get_database_schema()

        log_request_start(request_id, user_id, user_role, len(question))

        # LLM call — this is the slow part (can take 2-5 min on CPU Ollama)
        ai_logger.info(f"[AI-JOB] job_id={job_id} request_id={request_id} calling LLM...")
        generated_sql = ai_engine.generate_sql(
            question=question,
            schema=schema,
            user_role=user_role,
            user_context=user_context
        )

        # Clean + validate SQL
        generated_sql = sql_validator.clean_sql(generated_sql)
        is_valid, error_msg = sql_validator.validate(generated_sql)

        if not is_valid:
            execution_time = int((time.time() - start_time) * 1000)
            db.query(AIQueryHistory).filter(AIQueryHistory.id == job_id).update({
                "job_status": "failed",
                "success": False,
                "error_message": f"Invalid SQL: {error_msg}",
                "generated_sql": generated_sql,
                "execution_time_ms": execution_time,
                "ai_provider": ai_engine.name,
            })
            db.commit()
            ai_logger.warning(f"[AI-JOB] job_id={job_id} SQL validation failed: {error_msg}")
            return

        # Add role-based filters
        optimized_sql = query_optimizer.add_role_filters(generated_sql, user_role, user_context)

        # Execute query against DB
        try:
            result = db.execute(text(optimized_sql))
            rows = result.fetchall()
            columns = result.keys()
            data = [dict(zip(columns, row)) for row in rows]
        except Exception as db_error:
            db.rollback()
            raise db_error

        # Generate chart config
        visualization = chart_generator.generate_chart_config(data, optimized_sql, question)

        execution_time = int((time.time() - start_time) * 1000)

        # Save result — mark completed
        db.query(AIQueryHistory).filter(AIQueryHistory.id == job_id).update({
            "job_status": "completed",
            "success": True,
            "generated_sql": optimized_sql,
            "execution_time_ms": execution_time,
            "result_count": len(data),
            "ai_provider": ai_engine.name,
            "response_data": json.dumps(data, default=str),
            "visualization_config": json.dumps(visualization, default=str) if visualization else None,
        })
        db.commit()

        log_request_complete(request_id, execution_time, len(data), ai_engine.name)
        ai_logger.info(f"[AI-JOB] job_id={job_id} completed in {execution_time}ms, rows={len(data)}")

    except Exception as e:
        execution_time = int((time.time() - start_time) * 1000)
        ai_logger.error(f"[AI-JOB] job_id={job_id} FAILED: {type(e).__name__}: {e}")
        try:
            db.rollback()
            db.query(AIQueryHistory).filter(AIQueryHistory.id == job_id).update({
                "job_status": "failed",
                "success": False,
                "error_message": f"{type(e).__name__}: {str(e)}",
                "execution_time_ms": execution_time,
            })
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


@router.post("/ask")
async def ask_ai_report(
    question: str,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Submit an AI report question.
    Returns job_id instantly — result is processed in background.
    Poll GET /status/{job_id} to get the result.
    """
    if not os.getenv("AI_ENABLED", "true").lower() == "true":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI reporting is currently disabled"
        )

    if current_user.role not in ["district", "block", "sub_centre"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI reporting is only available for district, block, and sub-centre users"
        )

    user_context = {
        "district_id": current_user.district_id,
        "block_id": current_user.block_id,
        "sub_centre_id": current_user.sub_centre_id,
    }

    # Create job record with status=processing — returns immediately
    try:
        history = AIQueryHistory(
            user_id=current_user.id,
            user_role=current_user.role,
            natural_query=question,
            job_status="processing",
            success=False,
        )
        db.add(history)
        db.commit()
        db.refresh(history)
        job_id = history.id
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create job: {str(e)}")

    ai_logger.info(f"[AI-JOB] job_id={job_id} created for user={current_user.id} question='{question[:60]}'")

    # Fire background task — no await, returns instantly to client
    background_tasks.add_task(
        _run_ai_job,
        job_id=job_id,
        question=question,
        user_id=current_user.id,
        user_role=current_user.role,
        user_context=user_context,
    )

    return {
        "success": True,
        "job_id": job_id,
        "status": "processing",
        "message": "Your question is being processed. Poll /status/{job_id} for the result.",
        "question": question,
    }


@router.get("/status/{job_id}")
async def get_job_status(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Poll job status. Returns:
      - status=processing: still running, poll again in 3-5 seconds
      - status=completed: result is ready
      - status=failed: something went wrong
    """
    record = db.query(AIQueryHistory).filter(
        AIQueryHistory.id == job_id,
        AIQueryHistory.user_id == current_user.id  # users can only see their own jobs
    ).first()

    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    # Still running
    if record.job_status == "processing":
        return {
            "job_id": job_id,
            "status": "processing",
            "message": "Still processing, please wait...",
            "question": record.natural_query,
        }

    # Failed
    if record.job_status == "failed":
        return {
            "job_id": job_id,
            "status": "failed",
            "success": False,
            "message": record.error_message or "AI processing failed",
            "question": record.natural_query,
            "generated_sql": record.generated_sql,
        }

    # Completed — return full result
    return {
        "job_id": job_id,
        "status": "completed",
        "success": True,
        "question": record.natural_query,
        "generated_sql": record.generated_sql,
        "data": json.loads(record.response_data) if record.response_data else [],
        "result_count": record.result_count or 0,
        "execution_time_ms": record.execution_time_ms,
        "ai_provider": record.ai_provider,
        "visualization": json.loads(record.visualization_config) if record.visualization_config else None,
    }


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
    query = db.query(AIQueryHistory).filter(
        AIQueryHistory.user_id == current_user.id
    )

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

    formatted_history = []
    for record in history_records:
        formatted_history.append({
            "id": record.id,
            "question": record.natural_query,
            "status": record.job_status,
            "success": record.success,
            "result_count": record.result_count,
            "execution_time_ms": record.execution_time_ms,
            "ai_provider": record.ai_provider,
            "created_at": record.created_at,
            "data": json.loads(record.response_data) if record.response_data else None,
            "visualization": json.loads(record.visualization_config) if record.visualization_config else None,
            "generated_sql": record.generated_sql,
            "error_message": record.error_message,
        })

    return {
        "success": True,
        "history": formatted_history,
        "total": len(formatted_history),
        "filters": {"start_date": start_date, "end_date": end_date}
    }


@router.get("/engines")
async def get_available_engines(
    current_user: User = Depends(get_current_active_user)
):
    """Get list of available AI engines"""
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only district users can reload engine")

    try:
        ai_factory.reload_engine()
        return {
            "success": True,
            "message": "AI engine reloaded successfully",
            "current_provider": os.getenv("AI_PROVIDER")
        }
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error reloading engine: {str(e)}")
