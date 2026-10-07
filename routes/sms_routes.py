from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, date

from database import get_db
from models import User, SMSLog, PregnantWoman
from auth import get_current_active_user
from sms_service import sms_service
from sms_templates import get_sms_template, get_available_templates

router = APIRouter(prefix="/sms", tags=["SMS Management"])

@router.post("/send")
async def send_sms(
    mobile_number: str,
    template_name: str,
    template_variables: Optional[dict] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Send SMS using predefined templates
    """
    if current_user.role not in ["district", "block", "sub_centre"]:
        raise HTTPException(status_code=403, detail="Not authorized to send SMS")
    
    try:
        # Get message from template
        variables = template_variables or {}
        _sms = get_sms_template(template_name, **variables)
        
        # Send SMS
        result = await sms_service.send_sms(
            mobile_number=mobile_number,
            message=_sms["message"],
            message_type=template_name,
            db=db,
            template_id=_sms["template_id"]
        )
        
        return {
            "success": result["success"],
            "message": "SMS sent successfully" if result["success"] else result["message"],
            "template_used": template_name
        }
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send SMS: {str(e)}")

@router.post("/send-pregnancy-registration")
async def send_pregnancy_registration_sms(
    pregnant_woman_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Send pregnancy registration confirmation SMS
    """
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pregnant_woman_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")
    
    _sms = get_sms_template("pregnancy_registration")
    
    result = await sms_service.send_sms(
        mobile_number=pw.mobile_number,
        message=_sms["message"],
        message_type="pregnancy_registration",
        pregnant_woman_id=pw.id,
        db=db,
        template_id=_sms["template_id"]
    )
    
    return {"success": result["success"], "message": result.get("message", "SMS sent")}

@router.post("/send-anc-reminder")
async def send_anc_reminder_sms(
    pregnant_woman_id: int,
    visit_date: str,
    facility_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Send ANC visit reminder SMS
    """
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pregnant_woman_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")
    
    _sms = get_sms_template("anc_reminder", DATE=visit_date, FACILITY=facility_name)
    
    result = await sms_service.send_sms(
        mobile_number=pw.mobile_number,
        message=_sms["message"],
        message_type="anc_reminder",
        pregnant_woman_id=pw.id,
        db=db,
        template_id=_sms["template_id"]
    )
    
    return {"success": result["success"], "message": result.get("message", "SMS sent")}

@router.post("/send-usg-confirmation")
async def send_usg_confirmation_sms(
    pregnant_woman_id: int,
    appointment_date: str,
    usg_centre_name: str,
    usg_appointment_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Send USG booking confirmation SMS
    """
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pregnant_woman_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")
    
    _sms = get_sms_template("usg_booking_confirmation", DATE=appointment_date, USG_CENTRE=usg_centre_name)
    
    result = await sms_service.send_sms(
        mobile_number=pw.mobile_number,
        message=_sms["message"],
        message_type="usg_booking_confirmation",
        pregnant_woman_id=pw.id,
        usg_appointment_id=usg_appointment_id,
        db=db,
        template_id=_sms["template_id"]
    )
    
    return {"success": result["success"], "message": result.get("message", "SMS sent")}

@router.post("/send-high-risk-alert")
async def send_high_risk_alert_sms(
    pregnant_woman_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Send high-risk pregnancy alert SMS
    """
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pregnant_woman_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")
    
    _sms = get_sms_template("high_risk_alert")
    
    result = await sms_service.send_sms(
        mobile_number=pw.mobile_number,
        message=_sms["message"],
        message_type="high_risk_alert",
        pregnant_woman_id=pw.id,
        db=db,
        template_id=_sms["template_id"]
    )
    
    return {"success": result["success"], "message": result.get("message", "SMS sent")}

@router.post("/send-usg-feedback-notification")
async def send_usg_feedback_sms(
    pregnant_woman_id: int,
    usg_appointment_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Send USG completed feedback notification SMS
    """
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pregnant_woman_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")
    
    _sms = get_sms_template("usg_completed_feedback")
    
    result = await sms_service.send_sms(
        mobile_number=pw.mobile_number,
        message=_sms["message"],
        message_type="usg_completed_feedback",
        pregnant_woman_id=pw.id,
        usg_appointment_id=usg_appointment_id,
        db=db,
        template_id=_sms["template_id"]
    )
    
    return {"success": result["success"], "message": result.get("message", "SMS sent")}

@router.post("/send-grievance-acknowledgement")
async def send_grievance_acknowledgement_sms(
    mobile_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Send grievance acknowledgement SMS
    """
    _sms = get_sms_template("grievance_acknowledgement")
    
    result = await sms_service.send_sms(
        mobile_number=mobile_number,
        message=_sms["message"],
        message_type="grievance_acknowledgement",
        db=db,
        template_id=_sms["template_id"]
    )
    
    return {"success": result["success"], "message": result.get("message", "SMS sent")}

@router.get("/templates")
async def get_sms_templates(
    current_user: User = Depends(get_current_active_user)
):
    """
    Get available SMS templates
    """
    return {
        "templates": get_available_templates(),
        "total_templates": len(get_available_templates())
    }

@router.get("/logs")
async def get_sms_logs(
    skip: int = 0,
    limit: int = 100,
    message_type: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get SMS delivery logs
    """
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=403, detail="Not authorized to view SMS logs")
    
    query = db.query(SMSLog)
    
    if message_type:
        query = query.filter(SMSLog.message_type == message_type)
    if start_date:
        query = query.filter(SMSLog.created_at >= start_date)
    if end_date:
        query = query.filter(SMSLog.created_at <= end_date)
    
    logs = query.order_by(SMSLog.created_at.desc()).offset(skip).limit(limit).all()
    
    return {
        "logs": logs,
        "total": query.count()
    }

@router.get("/statistics")
async def get_sms_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get SMS usage statistics
    """
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=403, detail="Not authorized to view SMS statistics")
    
    total_sms = db.query(SMSLog).count()
    sent_sms = db.query(SMSLog).filter(SMSLog.delivery_status == "sent").count()
    failed_sms = db.query(SMSLog).filter(SMSLog.delivery_status == "failed").count()
    
    # SMS by type
    from sqlalchemy import func
    sms_by_type = db.query(
        SMSLog.message_type,
        func.count(SMSLog.id).label('count')
    ).group_by(SMSLog.message_type).all()
    
    return {
        "total_sms": total_sms,
        "sent_sms": sent_sms,
        "failed_sms": failed_sms,
        "success_rate": round((sent_sms / total_sms * 100) if total_sms > 0 else 0, 2),
        "sms_by_type": [{"type": item.message_type, "count": item.count} for item in sms_by_type]
    }