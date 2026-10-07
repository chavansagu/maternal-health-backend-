"""
IVR Routes - High Risk Advisory Calls
Endpoints:
  - GET  /api/v2/ivr/call-logs         — view call logs (district/block)
  - POST /api/v2/ivr/trigger-high-risk — manually trigger high-risk calls (district)
  - POST /api/v2/ivr/webhook           — Dovesoft callback when call completes
"""
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timedelta
import logging

from database import get_db
from models import IVRCallLog, PregnantWoman, User
from auth import get_current_active_user
from ivr_service import trigger_high_risk_call
import requests
import os

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ivr", tags=["IVR Calls"])


# ─── GET: Call Logs ──────────────────────────────────────────────────────────

@router.get("/call-logs")
async def get_call_logs(
    call_type: Optional[str] = None,
    call_status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get IVR call logs — district and block users only"""
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=403, detail="Access denied")

    query = db.query(IVRCallLog)

    # Role-based scope
    if current_user.role == "district":
        pw_ids = db.query(PregnantWoman.id).filter(
            PregnantWoman.district_id == current_user.district_id
        ).subquery()
        query = query.filter(IVRCallLog.pregnant_woman_id.in_(pw_ids))
    elif current_user.role == "block":
        pw_ids = db.query(PregnantWoman.id).filter(
            PregnantWoman.block_id == current_user.block_id
        ).subquery()
        query = query.filter(IVRCallLog.pregnant_woman_id.in_(pw_ids))

    if call_type:
        query = query.filter(IVRCallLog.call_type == call_type)
    if call_status:
        query = query.filter(IVRCallLog.call_status == call_status)

    total = query.count()
    logs = query.order_by(IVRCallLog.created_at.desc()).offset(skip).limit(limit).all()

    result = []
    for log in logs:
        pw = db.query(PregnantWoman).filter(PregnantWoman.id == log.pregnant_woman_id).first()
        
        # Parse provider response to get detailed call info
        call_details = {}
        if log.provider_response:
            try:
                import json
                provider_data = json.loads(log.provider_response)
                if isinstance(provider_data, dict):
                    call_details = {
                        "caller_id": provider_data.get("Caller ID", "-"),
                        "sent_date": provider_data.get("Sent Date", "-"),
                        "call_connect": provider_data.get("Call Connect", "-"),
                        "call_end": provider_data.get("Call End", "-"),
                        "campaign_id": provider_data.get("Campaign ID", "-")
                    }
            except:
                pass
        
        result.append({
            "id": log.id,
            "pregnant_woman_id": log.pregnant_woman_id,
            "pregnant_woman_name": pw.full_name if pw else None,
            "mobile_number": log.mobile_number,
            "call_type": log.call_type,
            "call_status": log.call_status,
            "ivr_campaign_id": log.ivr_campaign_id,
            "call_duration_seconds": log.call_duration_seconds,
            "scheduled_at": log.scheduled_at,
            "called_at": log.called_at,
            "created_at": log.created_at,
            # Additional Dovesoft details
            "caller_id": call_details.get("caller_id", "-"),
            "sent_date": call_details.get("sent_date", "-"),
            "call_connect": call_details.get("call_connect", "-"),
            "call_end": call_details.get("call_end", "-"),
            "dovesoft_campaign_id": call_details.get("campaign_id", "-")
        })

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "data": result
    }


# ─── GET: Call Logs for specific PW ─────────────────────────────────────────

@router.get("/call-logs/{pw_id}")
async def get_call_logs_for_pw(
    pw_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get IVR call history for a specific pregnant woman"""
    if current_user.role not in ["district", "block", "sub_centre"]:
        raise HTTPException(status_code=403, detail="Access denied")

    logs = db.query(IVRCallLog).filter(
        IVRCallLog.pregnant_woman_id == pw_id
    ).order_by(IVRCallLog.created_at.desc()).all()

    return [
        {
            "id": log.id,
            "call_type": log.call_type,
            "call_status": log.call_status,
            "ivr_campaign_id": log.ivr_campaign_id,
            "call_duration_seconds": log.call_duration_seconds,
            "called_at": log.called_at,
            "created_at": log.created_at,
        }
        for log in logs
    ]


# ─── POST: Manually Trigger High-Risk Advisory Calls ────────────────────────

@router.post("/trigger-high-risk")
async def trigger_high_risk_advisory(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Manually trigger high-risk advisory calls for all active high-risk women.
    District users only. Skips women called in last 7 days.
    """
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="Only district users can trigger IVR calls")

    seven_days_ago = datetime.now() - timedelta(days=7)

    # Get all active high-risk women in this district
    high_risk_women = db.query(PregnantWoman).filter(
        PregnantWoman.district_id == current_user.district_id,
        PregnantWoman.is_high_risk == True,
        PregnantWoman.is_active == True
    ).all()

    triggered = 0
    skipped = 0
    failed = 0

    for pw in high_risk_women:
        # Skip if already called in last 7 days
        recent_call = db.query(IVRCallLog).filter(
            IVRCallLog.pregnant_woman_id == pw.id,
            IVRCallLog.call_type == "high_risk_advisory",
            IVRCallLog.created_at >= seven_days_ago
        ).first()

        if recent_call:
            skipped += 1
            continue

        # Trigger call
        result = trigger_high_risk_call(pw.mobile_number, pw.id)

        # Save log
        call_log = IVRCallLog(
            pregnant_woman_id=pw.id,
            mobile_number=pw.mobile_number,
            call_type="high_risk_advisory",
            call_status="initiated" if result["success"] else "failed",
            ivr_campaign_id=result.get("campaign_id"),
            provider_response=result.get("raw_response"),
            called_at=datetime.now() if result["success"] else None,
        )
        db.add(call_log)

        if result["success"]:
            triggered += 1
        else:
            failed += 1

    db.commit()

    logger.info(
        f"[IVR] Manual trigger by district user {current_user.id} — "
        f"triggered={triggered}, skipped={skipped}, failed={failed}"
    )

    return {
        "message": f"High-risk advisory calls triggered",
        "triggered": triggered,
        "skipped_already_called": skipped,
        "failed": failed,
        "total_high_risk": len(high_risk_women)
    }


# ─── POST: Manually Trigger Feedback Calls ─────────────────────────────────

@router.post("/trigger-feedback-call-1/{usg_appointment_id}")
async def trigger_feedback_call_1_manual(
    usg_appointment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Manually trigger Feedback Call 1 for a specific USG appointment.
    District and block users only.
    """
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=403, detail="Access denied")

    from models import USGAppointment
    from ivr_service import trigger_feedback_call_1

    # Get the USG appointment
    appointment = db.query(USGAppointment).filter(
        USGAppointment.id == usg_appointment_id,
        USGAppointment.status == "completed"
    ).first()

    if not appointment:
        raise HTTPException(status_code=404, detail="Completed USG appointment not found")

    # Check if feedback call 1 already made
    existing_call = db.query(IVRCallLog).filter(
        IVRCallLog.pregnant_woman_id == appointment.pregnant_woman_id,
        IVRCallLog.call_type == "feedback_call_1"
    ).first()

    if existing_call:
        raise HTTPException(status_code=400, detail="Feedback Call 1 already made for this appointment")

    # Trigger the call
    result = trigger_feedback_call_1(
        appointment.pregnant_woman.mobile_number,
        appointment.pregnant_woman_id,
        appointment.id
    )

    # Log the call
    call_log = IVRCallLog(
        pregnant_woman_id=appointment.pregnant_woman_id,
        mobile_number=appointment.pregnant_woman.mobile_number,
        call_type="feedback_call_1",
        call_status="initiated" if result["success"] else "failed",
        ivr_campaign_id=result.get("campaign_id"),
        provider_response=result.get("raw_response"),
        called_at=datetime.now() if result["success"] else None,
    )
    db.add(call_log)
    db.commit()

    logger.info(
        f"[IVR] Manual Feedback Call 1 triggered by user {current_user.id} "
        f"for USG appointment {usg_appointment_id} — success={result['success']}"
    )

    return {
        "message": "Feedback Call 1 triggered" if result["success"] else "Feedback Call 1 failed",
        "success": result["success"],
        "campaign_id": result.get("campaign_id"),
        "error": result.get("error")
    }


@router.post("/trigger-feedback-call-2/{pregnant_woman_id}")
async def trigger_feedback_call_2_manual(
    pregnant_woman_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Manually trigger Feedback Call 2 for a specific pregnant woman.
    District and block users only.
    """
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=403, detail="Access denied")

    from ivr_service import trigger_feedback_call_2

    # Get the pregnant woman
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pregnant_woman_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")

    # Check if there was a failed/no-answer Call 1
    call_1_log = db.query(IVRCallLog).filter(
        IVRCallLog.pregnant_woman_id == pregnant_woman_id,
        IVRCallLog.call_type == "feedback_call_1",
        IVRCallLog.call_status.in_(["failed", "no_answer", "busy"])
    ).first()

    if not call_1_log:
        raise HTTPException(
            status_code=400, 
            detail="No failed/unanswered Feedback Call 1 found for this woman"
        )

    # Check if Call 2 already made
    existing_call_2 = db.query(IVRCallLog).filter(
        IVRCallLog.pregnant_woman_id == pregnant_woman_id,
        IVRCallLog.call_type == "feedback_call_2"
    ).first()

    if existing_call_2:
        raise HTTPException(status_code=400, detail="Feedback Call 2 already made for this woman")

    # Trigger the call
    result = trigger_feedback_call_2(
        pw.mobile_number,
        pw.id,
        None  # No specific USG appointment for Call 2
    )

    # Log the call
    call_log = IVRCallLog(
        pregnant_woman_id=pw.id,
        mobile_number=pw.mobile_number,
        call_type="feedback_call_2",
        call_status="initiated" if result["success"] else "failed",
        ivr_campaign_id=result.get("campaign_id"),
        provider_response=result.get("raw_response"),
        called_at=datetime.now() if result["success"] else None,
    )
    db.add(call_log)
    db.commit()

    logger.info(
        f"[IVR] Manual Feedback Call 2 triggered by user {current_user.id} "
        f"for pregnant woman {pregnant_woman_id} — success={result['success']}"
    )

    return {
        "message": "Feedback Call 2 triggered" if result["success"] else "Feedback Call 2 failed",
        "success": result["success"],
        "campaign_id": result.get("campaign_id"),
        "error": result.get("error")
    }


# ─── POST: Dovesoft Webhook ──────────────────────────────────────────────────

@router.post("/webhook")
async def ivr_webhook(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Webhook called by Dovesoft when a call completes.
    Updates IVRCallLog with final call status and duration.
    No authentication — Dovesoft calls this directly.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = dict(await request.form())

    logger.info(f"[IVR] Webhook received: {payload}")

    # Extract data from Dovesoft webhook payload
    unique_id = str(payload.get("uniqueId") or payload.get("campaignId") or payload.get("campaign_id") or "")
    mobile = str(payload.get("mobileNumber") or payload.get("mobile") or payload.get("contact") or payload.get("Mobile Number") or "").replace("+91", "").replace("+", "")
    status_val = str(payload.get("status") or payload.get("callStatus") or payload.get("Report") or "").lower()
    duration = payload.get("duration") or payload.get("callDuration") or payload.get("Answer Duration") or 0

    # Map Dovesoft status to our status
    status_map = {
        "answered": "connected",
        "connected": "connected",
        "not answered": "no_answer",
        "no answer": "no_answer",
        "noanswer": "no_answer",
        "not_answered": "no_answer",
        "unanswered": "no_answer",
        "busy": "busy",
        "failed": "failed",
        "rejected": "failed",
        "other": "failed",
        "congestion": "failed",
        "unprocessed": "initiated",
        "report pending": "initiated",
    }
    mapped_status = status_map.get(status_val, status_val)

    # Find the call log by uniqueId or mobile
    call_log = None
    if unique_id:
        call_log = db.query(IVRCallLog).filter(
            IVRCallLog.ivr_campaign_id == unique_id
        ).first()

    if not call_log and mobile:
        # Fallback: find most recent initiated call for this mobile
        call_log = db.query(IVRCallLog).filter(
            IVRCallLog.mobile_number == mobile,
            IVRCallLog.call_status == "initiated"
        ).order_by(IVRCallLog.created_at.desc()).first()

    if call_log:
        call_log.call_status = mapped_status
        call_log.call_duration_seconds = int(duration) if duration else None
        call_log.provider_response = str(payload)
        db.commit()
        logger.info(f"[IVR] Webhook updated call_log id={call_log.id} → status={mapped_status}")
    else:
        logger.warning(f"[IVR] Webhook: no matching call_log found for uniqueId={unique_id} mobile={mobile}")

    return {"status": "ok"}


@router.post("/test-feedback-call-1")
async def test_feedback_call_1(
    mobile_number: str,
    pregnant_woman_id: int,
    usg_appointment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    TEST ENDPOINT: Direct trigger for Feedback Call 1
    Remove this in production!
    """
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=403, detail="Access denied")

    from ivr_service import trigger_feedback_call_1

    # Trigger the call directly
    result = trigger_feedback_call_1(mobile_number, pregnant_woman_id, usg_appointment_id)

    # Log the call
    call_log = IVRCallLog(
        pregnant_woman_id=pregnant_woman_id,
        mobile_number=mobile_number,
        call_type="feedback_call_1",
        call_status="initiated" if result["success"] else "failed",
        ivr_campaign_id=result.get("campaign_id"),
        provider_response=result.get("raw_response"),
        called_at=datetime.now() if result["success"] else None,
    )
    db.add(call_log)
    db.commit()

    return {
        "message": "TEST: Feedback Call 1 triggered",
        "success": result["success"],
        "campaign_id": result.get("campaign_id"),
        "error": result.get("error"),
        "call_log_id": call_log.id
    }


@router.get("/test-announcements")
async def test_available_announcements(
    current_user: User = Depends(get_current_active_user)
):
    """
    TEST: Check what announcement IDs are available in your Dovesoft account
    """
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="District users only")

    import requests
    import json

    base_url = os.getenv("IVR_API_BASE_URL", "https://obd.dovesoft.ltd/vb-api/v2")
    user_id = os.getenv("IVR_USER_ID", "")
    user_token = os.getenv("IVR_USER_TOKEN", "")

    try:
        # Try to get announcements list
        params = {
            "userId": user_id,
            "userToken": user_token
        }
        
        response = requests.get(
            f"{base_url}/announcements",  # or whatever the correct endpoint is
            params=params,
            timeout=15
        )
        
        return {
            "status_code": response.status_code,
            "response": response.json(),
            "available_announcements": "Check the response for announcement IDs"
        }
    except Exception as e:
        return {
            "error": str(e),
            "message": "Could not fetch announcements. Use announcement ID 4825 for testing."
        }


@router.post("/test-dovesoft-api")
async def test_dovesoft_api(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Test endpoint to verify Dovesoft API connectivity and response format.
    District users only.
    """
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="Only district users can test API")

    # Dovesoft config
    base_url = os.getenv("IVR_API_BASE_URL", "https://obd.dovesoft.ltd/vb-api/v2")
    user_id = os.getenv("IVR_USER_ID", "")
    user_token = os.getenv("IVR_USER_TOKEN", "")

    test_results = {}

    # Test 1: getBulkCallDetailApi with empty params
    try:
        import json
        params = {
            "userId": user_id,
            "userToken": user_token,
            "params": json.dumps([{}])
        }
        headers = {"session-token": user_token}
        
        response = requests.get(
            f"{base_url}/campaigns/getBulkCallDetailApi",
            params=params,
            headers=headers,
            timeout=15
        )
        
        test_results["getBulkCallDetailApi_empty"] = {
            "status_code": response.status_code,
            "response": response.json(),
            "success": response.status_code == 200
        }
    except Exception as e:
        test_results["getBulkCallDetailApi_empty"] = {
            "error": str(e),
            "success": False
        }

    # Test 2: Get a sample uniqueId from existing logs
    sample_log = db.query(IVRCallLog).filter(
        IVRCallLog.ivr_campaign_id.isnot(None)
    ).first()
    
    if sample_log:
        try:
            params = {
                "userId": user_id,
                "userToken": user_token,
                "params": json.dumps([{"uniqueId": sample_log.ivr_campaign_id}])
            }
            headers = {"session-token": user_token}
            
            response = requests.get(
                f"{base_url}/campaigns/getBulkCallDetailApi",
                params=params,
                headers=headers,
                timeout=15
            )
            
            test_results["getBulkCallDetailApi_sample"] = {
                "status_code": response.status_code,
                "response": response.json(),
                "success": response.status_code == 200,
                "sample_uniqueId": sample_log.ivr_campaign_id
            }
        except Exception as e:
            test_results["getBulkCallDetailApi_sample"] = {
                "error": str(e),
                "success": False,
                "sample_uniqueId": sample_log.ivr_campaign_id
            }
    else:
        test_results["getBulkCallDetailApi_sample"] = {
            "message": "No sample uniqueId found in database",
            "success": False
        }

    return {
        "message": "Dovesoft API test completed",
        "config": {
            "base_url": base_url,
            "user_id": user_id,
            "token_configured": bool(user_token)
        },
        "test_results": test_results
    }

@router.post("/sync-status")
async def sync_call_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Sync call statuses from Dovesoft getBulkCallDetailApi.
    Fetches status for all 'initiated' call logs and updates them.
    District and block users only.
    """
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=403, detail="Access denied")

    # Dovesoft config
    base_url = os.getenv("IVR_API_BASE_URL", "https://obd.dovesoft.ltd/vb-api/v2")
    user_id = os.getenv("IVR_USER_ID", "")
    user_token = os.getenv("IVR_USER_TOKEN", "")

    status_map = {
        "answered": "connected",
        "not answered": "no_answer",
        "not_answered": "no_answer",
        "unanswered": "no_answer",
        "busy": "busy",
        "other": "failed",
        "congestion": "failed",
        "failed": "failed",
        "unprocessed": "initiated",
        "report pending": "initiated",
    }

    # Get all initiated logs scoped to current user
    query = db.query(IVRCallLog).filter(
        IVRCallLog.call_status == "initiated",
        IVRCallLog.ivr_campaign_id.isnot(None)
    )
    if current_user.role == "district":
        pw_ids = db.query(PregnantWoman.id).filter(
            PregnantWoman.district_id == current_user.district_id
        ).subquery()
        query = query.filter(IVRCallLog.pregnant_woman_id.in_(pw_ids))
    elif current_user.role == "block":
        pw_ids = db.query(PregnantWoman.id).filter(
            PregnantWoman.block_id == current_user.block_id
        ).subquery()
        query = query.filter(IVRCallLog.pregnant_woman_id.in_(pw_ids))

    pending_logs = query.all()
    updated = 0
    errors = 0

    if not pending_logs:
        return {
            "message": "No pending calls to sync",
            "updated": 0,
            "errors": 0,
            "total_checked": 0
        }

    # Prepare bulk request - group by campaign for efficiency
    unique_ids = []
    log_map = {}  # uniqueId -> log object
    
    for log in pending_logs:
        if log.ivr_campaign_id:
            unique_ids.append({"uniqueId": log.ivr_campaign_id})
            log_map[log.ivr_campaign_id] = log

    if not unique_ids:
        return {
            "message": "No valid campaign IDs to sync",
            "updated": 0,
            "errors": 0,
            "total_checked": 0
        }

    try:
        # Use getBulkCallDetailApi for batch processing
        import json
        params_json = json.dumps(unique_ids)
        
        params = {
            "userId": user_id,
            "userToken": user_token,
            "params": params_json
        }
        
        headers = {
            "session-token": user_token
        }
        
        response = requests.get(
            f"{base_url}/campaigns/getBulkCallDetailApi",
            params=params,
            headers=headers,
            timeout=30
        )
        
        data = response.json()
        logger.info(f"[IVR] Bulk sync response: {data}")

        if response.status_code == 200 and data.get("message") == "Records fetched successfully":
            results = data.get("results", [])
            
            for result in results:
                unique_id = result.get("uniqueId")
                if unique_id in log_map:
                    log = log_map[unique_id]
                    
                    # Extract call status
                    call_status_raw = str(result.get("Report", "")).lower()
                    duration = result.get("Answer Duration", 0)
                    
                    # Update log
                    if call_status_raw and call_status_raw not in ["initiated", "unprocessed", "report pending", ""]:
                        log.call_status = status_map.get(call_status_raw, "failed")
                        if duration:
                            log.call_duration_seconds = int(duration)
                        log.provider_response = json.dumps(result)
                        updated += 1
                        logger.info(f"[IVR] Updated log {log.id}: {call_status_raw} -> {log.call_status}")
        
        elif data.get("message") == "No records found":
            logger.info("[IVR] No records found in Dovesoft for the provided uniqueIds")
        else:
            logger.error(f"[IVR] Bulk sync failed: {data}")
            errors = len(pending_logs)

    except Exception as e:
        logger.error(f"[IVR] Bulk sync error: {str(e)}")
        errors = len(pending_logs)

    db.commit()
    logger.info(f"[IVR] Sync complete — updated={updated}, errors={errors}")

    return {
        "message": "Status sync complete",
        "updated": updated,
        "errors": errors,
        "total_checked": len(pending_logs)
    }
