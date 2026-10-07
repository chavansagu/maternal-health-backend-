from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, UploadFile, File, Form, Request
from starlette.datastructures import UploadFile as StarletteUploadFile
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta
import os
import uuid
import shutil
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

from database import get_db
from models import USGAppointment, User, PregnantWoman, Notification, SMSLog
from pydantic import BaseModel
from typing import List, Optional

class USGAppointmentCreateForm(BaseModel):
    """Schema for USG Appointment creation - for Swagger documentation only"""
    pregnant_woman_id: int
    usg_centre_id: int
    scheduled_date: str  # Format: YYYY-MM-DDTHH:MM:SS
    appointment_type: str = "regular"  # regular or emergency
    # Note: prescription_files are handled separately as multipart form data
    
    class Config:
        schema_extra = {
            "example": {
                "pregnant_woman_id": 1,
                "usg_centre_id": 1,
                "scheduled_date": "2026-05-10T10:00:00",
                "appointment_type": "regular"
            }
        }


class USGAppointmentRescheduleRequest(BaseModel):
    """Body schema for POST /{appointment_id}/reschedule (matches the JSON body the frontend sends)."""
    new_scheduled_date: datetime
    reschedule_reason: str
    is_emergency_override: bool = False
    override_reason: Optional[str] = None

from schemas import (
    USGAppointmentCreate, USGAppointmentResponse, 
    USGAppointmentUpdate, USGAppointmentComplete
)
from auth import get_current_active_user
from sms_service import sms_service
from sms_templates import get_sms_template
from file_validator import validate_upload_file
from audit_utils import get_client_info, get_entity_snapshot, log_create, log_update

from services.notification_service import NotificationService

def format_appointment_response(appointment, db=None):
    """Format appointment with file URLs and pregnant woman name"""
    import json
    pregnant_woman_name = None
    if db is not None:
        pw = db.query(PregnantWoman).filter(PregnantWoman.id == appointment.pregnant_woman_id).first()
        pregnant_woman_name = pw.full_name if pw else None

    # Build multi-file URL lists
    prescription_file_urls = []
    if appointment.prescription_file_paths:
        try:
            paths = json.loads(appointment.prescription_file_paths)
            prescription_file_urls = [f"/uploads/prescriptions/{os.path.basename(p)}" for p in paths]
        except Exception:
            pass

    report_file_urls = []
    if appointment.report_file_paths:
        try:
            paths = json.loads(appointment.report_file_paths)
            report_file_urls = [f"/uploads/usg_reports/{os.path.basename(p)}" for p in paths]
        except Exception:
            pass

    response_data = {
        "id": appointment.id,
        "pregnant_woman_id": appointment.pregnant_woman_id,
        "pregnant_woman_name": pregnant_woman_name,
        "usg_centre_id": appointment.usg_centre_id,
        "scheduled_date": appointment.scheduled_date,
        "appointment_type": appointment.appointment_type,
        "status": appointment.status,
        "reschedule_count": appointment.reschedule_count,
        "completed_date": appointment.completed_date,
        "sms_sent": appointment.sms_sent,
        "created_at": appointment.created_at,
        # scan details
        "scan_type": appointment.scan_type,
        "trimester": appointment.trimester,
        "gestational_age": appointment.gestational_age,
        "findings": appointment.findings,
        "abnormal_findings": appointment.abnormal_findings,
        "doctor_name": appointment.doctor_name,
        "technician_name": appointment.technician_name,
        "usg_findings": appointment.usg_findings,
        "is_high_risk": appointment.is_high_risk,
        # backward compat — single file URL (first file)
        "prescription_file_url": f"/uploads/prescriptions/{os.path.basename(appointment.prescription_file_path)}" if appointment.prescription_file_path else None,
        "report_file_url": f"/uploads/usg_reports/{os.path.basename(appointment.report_file_path)}" if appointment.report_file_path else None,
        # multi-file URLs
        "prescription_file_urls": prescription_file_urls,
        "report_file_urls": report_file_urls,
    }
    return response_data

router = APIRouter(prefix="/usg-appointments", tags=["USG Appointments"])

def check_duplicate_appointment(db: Session, pregnant_woman_id: int, scheduled_date: datetime) -> dict:
    """
    Check if pregnant woman already has an active appointment on the same date
    Returns dict with duplicate status and existing appointment details
    """
    # Define same date range (same day, ignoring time)
    date_start = scheduled_date.replace(hour=0, minute=0, second=0, microsecond=0)
    date_end = scheduled_date.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    # Check for existing active appointments on same date
    existing = db.query(USGAppointment).filter(
        USGAppointment.pregnant_woman_id == pregnant_woman_id,
        USGAppointment.scheduled_date >= date_start,
        USGAppointment.scheduled_date <= date_end,
        USGAppointment.status.in_(["scheduled", "accepted", "rescheduled"])
    ).first()
    
    if existing:
        return {
            "has_duplicate": True,
            "existing_appointment_id": existing.id,
            "existing_status": existing.status,
            "existing_date": existing.scheduled_date,
            "usg_centre_id": existing.usg_centre_id
        }
    else:
        return {
            "has_duplicate": False,
            "existing_appointment_id": None,
            "existing_status": None,
            "existing_date": None,
            "usg_centre_id": None
        }

async def send_sms_notification(mobile_number: str, message: str, db: Session,
                               message_type: str = "appointment",
                               pregnant_woman_id: Optional[int] = None,
                               usg_appointment_id: Optional[int] = None,
                               template_id: Optional[str] = None):
    """
    Background task to send SMS notification using new SMS service
    """
    result = await sms_service.send_sms(
        mobile_number=mobile_number,
        message=message,
        message_type=message_type,
        pregnant_woman_id=pregnant_woman_id,
        usg_appointment_id=usg_appointment_id,
        db=db,
        template_id=template_id
    )
    return result

async def create_notification(user_id: int, title: str, message: str, 
                             notification_type: str, reference_id: int, 
                             reference_type: str, db: Session):
    """
    Create notification for user
    """
    notification = Notification(
        user_id=user_id,
        title=title,
        message=message,
        notification_type=notification_type,
        reference_id=reference_id,
        reference_type=reference_type
    )
    db.add(notification)
    db.commit()

async def _save_single_file(file: UploadFile, upload_dir: str, file_type: str) -> str:
    """Validate and save a single file, return its path"""
    logger.info(f"[FILE SAVE] Validating file: {file.filename}, type: {file_type}")
    
    try:
        await validate_upload_file(file, file_type)
        logger.info(f"[FILE SAVE] File validation passed for: {file.filename}")
    except Exception as e:
        logger.error(f"[FILE SAVE] File validation failed for {file.filename}: {str(e)}")
        raise
    
    os.makedirs(upload_dir, exist_ok=True)
    file_extension = os.path.splitext(file.filename)[1]
    file_path = os.path.join(upload_dir, f"{uuid.uuid4()}{file_extension}")
    
    logger.info(f"[FILE SAVE] Saving file to: {file_path}")
    
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        logger.info(f"[FILE SAVE] Successfully saved file: {file_path}")
        return file_path
    except Exception as e:
        logger.error(f"[FILE SAVE] Error saving file {file.filename}: {str(e)}")
        # Clean up partial file if it exists
        if os.path.exists(file_path):
            os.remove(file_path)
        raise

async def save_usg_reports(files: List[UploadFile]) -> List[str]:
    """Save multiple USG report files, return list of paths"""
    return [await _save_single_file(f, "uploads/usg_reports", "report") for f in files]

async def save_prescriptions(files: List[UploadFile]) -> List[str]:
    """Save multiple prescription files, return list of paths"""
    logger.info(f"[SAVE PRESCRIPTIONS] Starting to save {len(files)} prescription files")
    paths = []
    
    for i, file in enumerate(files):
        logger.info(f"[SAVE PRESCRIPTIONS] Processing file {i+1}/{len(files)}: {file.filename}")
        try:
            path = await _save_single_file(file, "uploads/prescriptions", "prescription")
            paths.append(path)
            logger.info(f"[SAVE PRESCRIPTIONS] Successfully processed file {i+1}: {path}")
        except Exception as e:
            logger.error(f"[SAVE PRESCRIPTIONS] Failed to process file {i+1} ({file.filename}): {str(e)}")
            # Clean up any successfully saved files if one fails
            for saved_path in paths:
                if os.path.exists(saved_path):
                    os.remove(saved_path)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to save prescription file '{file.filename}': {str(e)}"
            )
    
    logger.info(f"[SAVE PRESCRIPTIONS] Successfully saved all {len(paths)} prescription files")
    return paths

@router.post("/", response_model=USGAppointmentResponse, status_code=status.HTTP_201_CREATED)
async def schedule_usg_appointment(
    pregnant_woman_id: int = Form(..., description="ID of the pregnant woman"),
    usg_centre_id: int = Form(..., description="ID of the USG centre"),
    scheduled_date: str = Form(..., description="Scheduled date and time (YYYY-MM-DDTHH:MM:SS)"),
    appointment_type: str = Form("regular", description="Type of appointment: regular or emergency"),
    prescription_files: List[UploadFile] = File(default=[], description="Multiple prescription files (.pdf, .jpg, .jpeg, .png)"),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Schedule a new USG appointment (PMSMA users only)

    This endpoint accepts multipart/form-data with the following fields:
    - pregnant_woman_id: ID of the pregnant woman (required)
    - usg_centre_id: ID of the USG centre (required)
    - scheduled_date: Date and time in YYYY-MM-DDTHH:MM:SS format (required)
    - appointment_type: "regular" or "emergency" (optional, default: "regular")
    - prescription_files: Multiple files allowed (.pdf, .jpg, .jpeg, .png)
    """
    if current_user.role not in ["block", "pmsma"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Block or PMSMA users can schedule USG appointments"
        )

    # Filter valid prescription files
    valid_prescription_files = [
        f for f in prescription_files 
        if f and f.filename and f.filename.strip() and isinstance(f, (UploadFile, StarletteUploadFile))
    ]
    
    logger.info(
        f"[USG SCHEDULE] Received | user={current_user.id} role={current_user.role} | "
        f"pw_id={pregnant_woman_id} usg_centre_id={usg_centre_id} "
        f"scheduled_date='{scheduled_date}' appointment_type='{appointment_type}' "
        f"prescription_files_count={len(valid_prescription_files)} "
        f"prescription_files_names={[f.filename for f in valid_prescription_files]}"
    )

    # Parse date with fallback for missing seconds (datetime-local gives HH:MM without seconds)
    try:
        if len(scheduled_date) == 16:  # "YYYY-MM-DDTHH:MM"
            scheduled_date = scheduled_date + ":00"
        scheduled_datetime = datetime.fromisoformat(scheduled_date)
    except ValueError as e:
        logger.error(f"[USG SCHEDULE] Invalid scheduled_date format: '{scheduled_date}' — {e}")
        raise HTTPException(status_code=400, detail=f"Invalid scheduled_date format: '{scheduled_date}'. Expected YYYY-MM-DDTHH:MM or YYYY-MM-DDTHH:MM:SS")

    # Verify pregnant woman exists
    pw = db.query(PregnantWoman).filter(
        PregnantWoman.id == pregnant_woman_id
    ).first()
    if not pw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pregnant woman not found"
        )

    # Block USG scheduling if delivery already completed
    if not pw.is_active or pw.pregnancy_outcome is not None:
        logger.warning(f"Attempt to create USG appointment for inactive/delivered PW: {pw.id}")
        raise HTTPException(
            status_code=400,
            detail=f"Cannot schedule USG appointment. Beneficiary '{pw.full_name}' has already completed delivery (outcome: {pw.pregnancy_outcome or 'inactive'}). USG appointments are only allowed for active pregnancies."
        )

    # VALIDATION: Check for duplicate appointment on same date
    duplicate_check = check_duplicate_appointment(db, pregnant_woman_id, scheduled_datetime)
    
    if duplicate_check["has_duplicate"]:
        from models import USGCentre
        existing_centre = db.query(USGCentre).filter(
            USGCentre.id == duplicate_check["usg_centre_id"]
        ).first()
        
        raise HTTPException(
            status_code=409,  # 409 Conflict
            detail=f"Pregnant woman already has an active appointment on {duplicate_check['existing_date'].strftime('%d-%m-%Y')} "
                   f"at {existing_centre.name if existing_centre else 'USG Centre'} "
                   f"(Status: {duplicate_check['existing_status']}). "
                   f"Please cancel or reschedule the existing appointment first."
        )
    
    # Save prescription files if provided
    import json
    prescription_paths = []
    if valid_prescription_files:
        logger.info(f"[USG SCHEDULE] Processing {len(valid_prescription_files)} valid prescription files")
        try:
            prescription_paths = await save_prescriptions(valid_prescription_files)
            logger.info(f"[USG SCHEDULE] Successfully saved prescription files: {prescription_paths}")
        except Exception as e:
            logger.error(f"[USG SCHEDULE] Error saving prescription files: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Error saving prescription files: {str(e)}"
            )
    else:
        logger.info(f"[USG SCHEDULE] No prescription files provided")

    # Create appointment
    new_appointment = USGAppointment(
        pregnant_woman_id=pregnant_woman_id,
        usg_centre_id=usg_centre_id,
        scheduled_date=scheduled_datetime,
        appointment_type=appointment_type,
        original_scheduled_date=scheduled_datetime,
        prescription_file_path=prescription_paths[0] if prescription_paths else None,
        prescription_file_paths=json.dumps(prescription_paths) if prescription_paths else None,
        scheduled_by=current_user.id,
        status="scheduled"
    )
    
    db.add(new_appointment)
    db.commit()
    db.refresh(new_appointment)

    # Log the creation (without request info since we're using Form parameters)
    log_create(
        db,
        current_user.id,
        "USGAppointment",
        new_appointment.id,
        get_entity_snapshot(new_appointment),
        None,  # ip_address
        None   # user_agent
    )
    
    # Send in-app notification to USG centre
    is_emergency = appointment_type == "emergency"
    recipients = NotificationService.get_recipients_for_event(
        db,
        "appointment_emergency" if is_emergency else "appointment_scheduled",
        {"usg_centre_id": usg_centre_id}
    )
    
    if recipients:
        NotificationService.create_notification(
            db=db,
            user_ids=recipients,
            title="🚨 Emergency USG Appointment" if is_emergency else "New USG Appointment",
            message=f"New {'emergency ' if is_emergency else ''}appointment scheduled for {pw.full_name} on {scheduled_date}",
            notification_type="appointment_emergency" if is_emergency else "appointment_scheduled",
            category="appointment",
            priority="high" if is_emergency else "normal",
            reference_id=new_appointment.id,
            reference_type="usg_appointment",
            action_url=f"/usg-appointments/{new_appointment.id}",
            metadata={"pregnant_woman_name": pw.full_name, "scheduled_date": scheduled_date}
        )
    
    # Send notification to USG centre (old method - keeping for backward compatibility)
    from models import User as UserModel
    usg_users = db.query(UserModel).filter(
        UserModel.usg_centre_id == usg_centre_id,
        UserModel.is_active == True
    ).all()
    
    for usg_user in usg_users:
        notification_title = "New USG Appointment"
        if appointment_type == "emergency":
            notification_title = "🚨 EMERGENCY USG Appointment"
        
        background_tasks.add_task(
            create_notification,
            user_id=usg_user.id,
            title=notification_title,
            message=f"New USG appointment scheduled for {pw.full_name} on {scheduled_date}",
            notification_type="appointment",
            reference_id=new_appointment.id,
            reference_type="usg_appointment",
            db=db
        )
    
    # Send SMS to pregnant woman
    if pw:
        from models import USGCentre
        usg_centre = db.query(USGCentre).filter(USGCentre.id == usg_centre_id).first()
        usg_centre_name = usg_centre.name if usg_centre else "USG Centre"
        
        _sms = get_sms_template(
            "usg_booking_confirmation",
            DATE=scheduled_datetime.strftime('%d-%m-%Y %H:%M'),
            USG_CENTRE=usg_centre_name
        )
        background_tasks.add_task(
            send_sms_notification,
            pw.mobile_number,
            _sms["message"],
            db,
            "usg_booking_confirmation",
            pw.id,
            new_appointment.id,
            _sms["template_id"]
        )
    
    return format_appointment_response(new_appointment, db)

@router.post("/test-files", response_model=dict)
async def test_prescription_files(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Test endpoint to verify multiple prescription files upload
    """
    form = await request.form()
    
    logger.info(f"[TEST FILES] Form fields: {list(form.keys())}")
    
    prescription_files: List[UploadFile] = [
        f for f in form.getlist("prescription_files") if isinstance(f, (UploadFile, StarletteUploadFile)) and f.filename
    ]
    
    logger.info(f"[TEST FILES] Received {len(prescription_files)} files")
    
    for i, file in enumerate(prescription_files):
        logger.info(f"[TEST FILES] File {i+1}: {file.filename}, content_type: {file.content_type}")
    
    # Test saving files
    try:
        if prescription_files:
            paths = await save_prescriptions(prescription_files)
            return {
                "message": "Files uploaded successfully",
                "file_count": len(prescription_files),
                "file_names": [f.filename for f in prescription_files],
                "saved_paths": paths
            }
        else:
            return {
                "message": "No files received",
                "file_count": 0,
                "form_keys": list(form.keys()),
                "raw_files": [str(f) for f in form.getlist("prescription_files")]
            }
    except Exception as e:
        logger.error(f"[TEST FILES] Error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error processing files: {str(e)}"
        )
@router.get("/", response_model=List[dict])
async def get_usg_appointments(
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = None,
    usg_centre_id: Optional[int] = None,
    appointment_type: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get USG appointments with filters
    """
    query = db.query(USGAppointment)
    
    # Role-based filtering — matches same logic as GET /pregnant-women/
    from models import UserWardMapping, WardSubcentreMapping
    from sqlalchemy import or_
    if current_user.role == "usg_centre":
        query = query.filter(USGAppointment.usg_centre_id == current_user.usg_centre_id)
    elif current_user.role == "district":
        query = query.join(PregnantWoman).filter(PregnantWoman.district_id == current_user.district_id)
    elif current_user.role == "block":
        query = query.join(PregnantWoman).filter(PregnantWoman.block_id == current_user.block_id)
    elif current_user.role == "pmsma":
        # PMSMA sees appointments they scheduled within their block
        query = query.filter(USGAppointment.scheduled_by == current_user.id)
    elif current_user.role == "sub_centre":
        user_wards = db.query(UserWardMapping.ward_id).filter(UserWardMapping.user_id == current_user.id).all()
        if user_wards:
            ward_ids = [w.ward_id for w in user_wards]
            query = query.join(PregnantWoman).filter(PregnantWoman.ward_id.in_(ward_ids))
        else:
            mapped_ward_ids = db.query(WardSubcentreMapping.ward_id).filter(
                WardSubcentreMapping.sub_centre_id == current_user.sub_centre_id
            ).subquery()
            query = query.join(PregnantWoman).filter(
                or_(PregnantWoman.ward_id.in_(mapped_ward_ids), PregnantWoman.sub_centre_id == current_user.sub_centre_id)
            )
    
    # Additional filters
    if status:
        query = query.filter(USGAppointment.status == status)
    if usg_centre_id:
        query = query.filter(USGAppointment.usg_centre_id == usg_centre_id)
    if appointment_type:
        query = query.filter(USGAppointment.appointment_type == appointment_type)
    if start_date:
        query = query.filter(USGAppointment.scheduled_date >= start_date)
    if end_date:
        query = query.filter(USGAppointment.scheduled_date <= end_date)
    
    # Order by created_at descending (latest first)
    query = query.order_by(USGAppointment.created_at.desc())
    
    appointments = query.offset(skip).limit(limit).all()
    return [format_appointment_response(a, db) for a in appointments]

@router.get("/pregnant-woman/{pw_id}")
async def get_usg_appointments_for_pregnant_woman(
    pw_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get all USG appointments for a specific pregnant woman"""
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pw_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")

    # Authorization check — matches GET /anc-visits/pregnant-woman/{pw_id}
    if current_user.role == "district" and pw.district_id != current_user.district_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "block" and pw.block_id != current_user.block_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "sub_centre":
        if pw.sub_centre_id is not None and pw.sub_centre_id != current_user.sub_centre_id:
            if pw.block_id != current_user.block_id:
                raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "usg_centre":
        has_appointment = db.query(USGAppointment).filter(
            USGAppointment.pregnant_woman_id == pw_id,
            USGAppointment.usg_centre_id == current_user.usg_centre_id
        ).first()
        if not has_appointment:
            raise HTTPException(status_code=403, detail="Not authorized")

    appointments = db.query(USGAppointment).filter(
        USGAppointment.pregnant_woman_id == pw_id
    ).order_by(USGAppointment.scheduled_date).all()

    # Next upcoming appointment (soonest scheduled/rescheduled one still pending)
    next_appointment_date = None
    upcoming = [a for a in appointments if a.status in ("scheduled", "rescheduled") and a.scheduled_date]
    if upcoming:
        next_appointment_date = min(a.scheduled_date for a in upcoming)

    return {
        "pregnant_woman": {
            "id": pw.id,
            "full_name": pw.full_name,
            "mobile_number": pw.mobile_number,
            "edd_date": pw.edd_date,
            "is_high_risk": pw.is_high_risk
        },
        "appointments": [format_appointment_response(a, db) for a in appointments],
        "total_appointments": len(appointments),
        "next_appointment_date": next_appointment_date
    }

@router.get("/pending", response_model=List[USGAppointmentResponse])
async def get_pending_appointments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get pending USG appointments (for USG centre to accept/reschedule)
    """
    if current_user.role not in ["usg_centre", "pmsma"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only USG centre or PMSMA users can access this endpoint"
        )

    query = db.query(USGAppointment).filter(USGAppointment.status == "scheduled")
    if current_user.role == "usg_centre":
        query = query.filter(USGAppointment.usg_centre_id == current_user.usg_centre_id)
    else:
        # pmsma: only sessions they scheduled
        query = query.filter(USGAppointment.scheduled_by == current_user.id)

    appointments = query.all()
    return [format_appointment_response(a, db) for a in appointments]

@router.put("/{appointment_id}", response_model=dict)
async def update_usg_appointment(
    appointment_id: int,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Update USG appointment details (ANM/Sub-centre/Block users)
    """
    form = await request.form()
    scheduled_date = form.get("scheduled_date") or None
    appointment_type = form.get("appointment_type") or None
    notes = form.get("notes") or None
    prescription_files: List[UploadFile] = [
        f for f in form.getlist("prescription_files") if isinstance(f, (UploadFile, StarletteUploadFile)) and f.filename
    ]
    # Check user permissions
    if current_user.role not in ["sub_centre", "block", "usg_centre", "pmsma"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Sub-centre, Block, USG centre, or PMSMA users can update appointments"
        )
    
    # Get appointment
    appointment = db.query(USGAppointment).filter(
        USGAppointment.id == appointment_id
    ).first()
    
    if not appointment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Appointment not found"
        )
    
    # Check if appointment can be updated
    if appointment.status == "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot update completed appointment"
        )
    
    # Authorization check
    pw = db.query(PregnantWoman).filter(
        PregnantWoman.id == appointment.pregnant_woman_id
    ).first()
    
    if current_user.role == "sub_centre":
        if pw.sub_centre_id is not None and pw.sub_centre_id != current_user.sub_centre_id:
            if pw.block_id != current_user.block_id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and pw.block_id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "usg_centre" and appointment.usg_centre_id != current_user.usg_centre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "pmsma" and appointment.scheduled_by != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    # Check if appointment is within 24 hours (only emergency can be updated)
    if scheduled_date:
        new_date = datetime.fromisoformat(scheduled_date)
        time_diff = new_date - datetime.now()
        if time_diff.total_seconds() < 86400 and appointment.appointment_type != "emergency":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot update appointment within 24 hours unless it's emergency"
            )
    
    # Update fields
    old_values = get_entity_snapshot(appointment)
    updated_fields = []
    
    if scheduled_date:
        old_date = appointment.scheduled_date
        appointment.scheduled_date = datetime.fromisoformat(scheduled_date)
        updated_fields.append(f"Date changed from {old_date} to {scheduled_date}")
    
    if appointment_type and appointment_type in ["regular", "emergency"]:
        old_type = appointment.appointment_type
        appointment.appointment_type = appointment_type
        updated_fields.append(f"Type changed from {old_type} to {appointment_type}")
    
    if notes:
        # Add notes to existing doctor_notes or create new
        existing_notes = getattr(appointment, 'doctor_notes', '') or ''
        appointment.doctor_notes = f"{existing_notes}\n[Updated {datetime.now().strftime('%Y-%m-%d %H:%M')}]: {notes}"
        updated_fields.append("Notes added")
    
    # Handle prescription files update
    import json
    if prescription_files:
        valid_files = [f for f in prescription_files if f and f.filename]
        if valid_files:
            # Delete old files if they exist
            if appointment.prescription_file_paths:
                try:
                    old_paths = json.loads(appointment.prescription_file_paths)
                    for old_path in old_paths:
                        if os.path.exists(old_path):
                            os.remove(old_path)
                except Exception:
                    pass
            elif appointment.prescription_file_path and os.path.exists(appointment.prescription_file_path):
                os.remove(appointment.prescription_file_path)

            new_paths = await save_prescriptions(valid_files)
            appointment.prescription_file_path = new_paths[0]
            appointment.prescription_file_paths = json.dumps(new_paths)
            updated_fields.append("Prescription files updated")
    
    # Update timestamp
    appointment.updated_at = datetime.now()
    
    db.commit()
    db.refresh(appointment)

    ip_address, user_agent = get_client_info(request)
    log_update(
        db,
        current_user.id,
        "USGAppointment",
        appointment.id,
        old_values,
        get_entity_snapshot(appointment),
        ip_address,
        user_agent
    )
    
    # Send notification if date changed
    if scheduled_date:
        # Notify USG centre
        usg_users = db.query(User).filter(
            User.usg_centre_id == appointment.usg_centre_id,
            User.is_active == True
        ).all()
        
        for usg_user in usg_users:
            background_tasks.add_task(
                create_notification,
                user_id=usg_user.id,
                title="USG Appointment Updated",
                message=f"Appointment for {pw.full_name} has been updated. New date: {scheduled_date}",
                notification_type="appointment_update",
                reference_id=appointment.id,
                reference_type="usg_appointment",
                db=db
            )
        
        # Send SMS to pregnant woman
        if pw:
            _sms = get_sms_template(
                "usg_booking_confirmation",
                DATE=datetime.fromisoformat(scheduled_date).strftime('%d-%m-%Y %H:%M'),
                USG_CENTRE=appointment.usg_centre.name if appointment.usg_centre else "USG Centre"
            )
            background_tasks.add_task(
                send_sms_notification,
                pw.mobile_number,
                _sms["message"],
                db,
                "usg_booking_confirmation",
                pw.id,
                appointment.id,
                _sms["template_id"]
            )
    
    return {
        "message": "Appointment updated successfully",
        "appointment_id": appointment.id,
        "updated_fields": updated_fields,
        "appointment": format_appointment_response(appointment, db)
    }

@router.get("/{appointment_id}", response_model=dict)
async def get_appointment(
    appointment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get USG appointment details
    """
    appointment = db.query(USGAppointment).filter(
        USGAppointment.id == appointment_id
    ).first()
    
    if not appointment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Appointment not found"
        )
    
    return format_appointment_response(appointment, db)

@router.post("/{appointment_id}/accept", response_model=USGAppointmentResponse)
async def accept_appointment(
    appointment_id: int,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Accept USG appointment (USG centre only)
    """
    if current_user.role not in ["usg_centre"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only USG centre or PMSMA users can accept appointments"
        )

    appt_query = db.query(USGAppointment).filter(USGAppointment.id == appointment_id)
    if current_user.role == "usg_centre":
        appt_query = appt_query.filter(USGAppointment.usg_centre_id == current_user.usg_centre_id)
    else:
        appt_query = appt_query.filter(USGAppointment.scheduled_by == current_user.id)
    appointment = appt_query.first()
    
    if not appointment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Appointment not found"
        )
    
    if appointment.status != "scheduled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Appointment is not in scheduled status"
        )
    
    # Fetch pregnant woman details
    pw = db.query(PregnantWoman).filter(
        PregnantWoman.id == appointment.pregnant_woman_id
    ).first()
    
    # Update appointment status
    old_values = get_entity_snapshot(appointment)
    appointment.status = "accepted"
    appointment.accepted_by = current_user.id
    db.commit()
    db.refresh(appointment)
    
    # Send in-app notification to scheduler
    if appointment.scheduled_by and pw:
        NotificationService.create_notification(
            db=db,
            user_ids=[appointment.scheduled_by],
            title="USG Appointment Accepted",
            message=f"Your appointment for {pw.full_name} has been accepted by the USG centre",
            notification_type="appointment_accepted",
            category="appointment",
            priority="normal",
            reference_id=appointment.id,
            reference_type="usg_appointment",
            action_url=f"/usg-appointments/{appointment.id}",
            metadata={"pregnant_woman_name": pw.full_name}
        )
    
    # Send SMS to pregnant woman
    # TODO: Uncomment when dedicated accept SMS template is available
    # if pw:
    #     from models import USGCentre
    #     usg_centre = db.query(USGCentre).filter(USGCentre.id == appointment.usg_centre_id).first()
    #     _sms = get_sms_template(
    #         "usg_booking_confirmation",
    #         DATE=appointment.scheduled_date.strftime('%d-%m-%Y %H:%M'),
    #         USG_CENTRE=usg_centre.name if usg_centre else "USG Centre"
    #     )
    #     background_tasks.add_task(
    #         send_sms_notification,
    #         pw.mobile_number,
    #         _sms["message"],
    #         db,
    #         "usg_booking_confirmation",
    #         pw.id,
    #         appointment.id,
    #         _sms["template_id"]
    #     )
    #     appointment.sms_sent = True
    #     appointment.sms_sent_at = datetime.now()
    #     db.commit()

    ip_address, user_agent = get_client_info(request)
    log_update(
        db,
        current_user.id,
        "USGAppointment",
        appointment.id,
        old_values,
        get_entity_snapshot(appointment),
        ip_address,
        user_agent
    )
    
    return format_appointment_response(appointment, db)

@router.post("/{appointment_id}/reschedule", response_model=USGAppointmentResponse)
async def reschedule_appointment(
    appointment_id: int,
    body: USGAppointmentRescheduleRequest,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    request: Request = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Reschedule USG appointment (USG centre, within 7-day window or emergency override with reason)
    """
    new_scheduled_date = body.new_scheduled_date
    reschedule_reason = body.reschedule_reason
    is_emergency_override = body.is_emergency_override
    override_reason = body.override_reason

    if current_user.role not in ["usg_centre", "pmsma"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only USG centre or PMSMA users can reschedule appointments"
        )

    appt_query = db.query(USGAppointment).filter(USGAppointment.id == appointment_id)
    if current_user.role == "usg_centre":
        appt_query = appt_query.filter(USGAppointment.usg_centre_id == current_user.usg_centre_id)
    appointment = appt_query.first()

    if not appointment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Appointment not found"
        )

    from scheduler import check_reschedule_window
    window = check_reschedule_window(
        new_scheduled_date,
        appointment.original_scheduled_date,
        is_emergency_override,
        override_reason
    )

    # Fetch pregnant woman details
    pw = db.query(PregnantWoman).filter(
        PregnantWoman.id == appointment.pregnant_woman_id
    ).first()

    # Update appointment
    old_values = get_entity_snapshot(appointment)
    appointment.scheduled_date = new_scheduled_date
    appointment.status = "rescheduled"
    appointment.reschedule_count += 1
    appointment.reschedule_reason = reschedule_reason
    appointment.accepted_by = current_user.id
    db.commit()
    db.refresh(appointment)

    # Send in-app notification to scheduler
    if appointment.scheduled_by and pw:
        NotificationService.create_notification(
            db=db,
            user_ids=[appointment.scheduled_by],
            title="USG Appointment Rescheduled",
            message=f"Appointment for {pw.full_name} has been rescheduled to {new_scheduled_date.strftime('%d-%m-%Y %H:%M')}",
            notification_type="appointment_rescheduled",
            category="appointment",
            priority="normal",
            reference_id=appointment.id,
            reference_type="usg_appointment",
            action_url=f"/usg-appointments/{appointment.id}",
            metadata={"pregnant_woman_name": pw.full_name, "new_date": new_scheduled_date.isoformat()}
        )

    # Send SMS to pregnant woman
    if pw:
        from models import USGCentre
        usg_centre = db.query(USGCentre).filter(USGCentre.id == appointment.usg_centre_id).first()
        _sms = get_sms_template(
            "usg_booking_confirmation",
            DATE=new_scheduled_date.strftime('%d-%m-%Y %H:%M'),
            USG_CENTRE=usg_centre.name if usg_centre else "USG Centre"
        )
        background_tasks.add_task(
            send_sms_notification,
            pw.mobile_number,
            _sms["message"],
            db,
            "usg_booking_confirmation",
            pw.id,
            appointment.id,
            _sms["template_id"]
        )

        appointment.sms_sent = True
        appointment.sms_sent_at = datetime.now()
        db.commit()

    # Audit — log override distinctly from normal reschedule
    ip_address, user_agent = get_client_info(request) if request else (None, None)
    action_label = "EMERGENCY_OVERRIDE_RESCHEDULE" if window["is_override"] else "RESCHEDULE"
    log_update(
        db,
        current_user.id,
        "USGAppointment",
        appointment.id,
        old_values,
        {**get_entity_snapshot(appointment), "_action": action_label, "_override_reason": window["reason"]},
        ip_address,
        user_agent
    )

    return format_appointment_response(appointment, db)

@router.post("/{appointment_id}/complete", response_model=USGAppointmentResponse)
async def complete_appointment(
    appointment_id: int,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Mark USG appointment as completed and upload report (USG centre only)
    """
    form = await request.form()
    completed_date = str(form.get("completed_date", ""))
    scan_date = str(form.get("scan_date", ""))
    gestational_age = form.get("gestational_age") or None
    trimester = str(form.get("trimester", ""))
    scan_type = str(form.get("scan_type", ""))
    findings = str(form.get("findings", ""))
    abnormal_findings = form.get("abnormal_findings") or None
    additional_notes = form.get("additional_notes") or None
    doctor_name = str(form.get("doctor_name", ""))
    technician_name = str(form.get("technician_name", ""))
    usg_findings = form.get("usg_findings") or None
    is_high_risk = str(form.get("is_high_risk", "false")).lower() in ("true", "1", "yes")
    report_files: List[UploadFile] = [
        f for f in form.getlist("report_files") if isinstance(f, (UploadFile, StarletteUploadFile)) and f.filename
    ]
    if current_user.role not in ["usg_centre"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only USG centre users can complete appointments"
        )

    appt_query = db.query(USGAppointment).filter(USGAppointment.id == appointment_id)
    if current_user.role == "usg_centre":
        appt_query = appt_query.filter(USGAppointment.usg_centre_id == current_user.usg_centre_id)
    else:
        appt_query = appt_query.filter(USGAppointment.scheduled_by == current_user.id)
    appointment = appt_query.first()
    
    if not appointment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Appointment not found"
        )
    
    # Fetch pregnant woman details
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == appointment.pregnant_woman_id).first()

    # Validate confirmed_edd_date — required for USG completion
    confirmed_edd = form.get("confirmed_edd_date") or None
    if not confirmed_edd:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="confirmed_edd_date is required when completing a USG appointment"
        )
    from datetime import date as date_type
    try:
        confirmed_edd_parsed = date_type.fromisoformat(str(confirmed_edd))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="confirmed_edd_date must be in YYYY-MM-DD format"
        )

    # Validate at least one report file provided
    import json
    valid_report_files = [f for f in report_files if f and f.filename]

    # TODO: Uncomment to make report file compulsory again
    # if not valid_report_files:
    #     raise HTTPException(
    #         status_code=status.HTTP_400_BAD_REQUEST,
    #         detail="At least one report file is required"
    #     )

    # Save report files only if provided
    report_paths = await save_usg_reports(valid_report_files) if valid_report_files else []

    # Update appointment
    old_values = get_entity_snapshot(appointment)
    appointment.status = "completed"
    appointment.completed_date = datetime.fromisoformat(completed_date)
    appointment.scan_date = datetime.fromisoformat(scan_date).date()
    appointment.gestational_age = gestational_age
    appointment.trimester = trimester
    appointment.scan_type = scan_type
    appointment.findings = findings
    appointment.abnormal_findings = abnormal_findings
    appointment.additional_notes = additional_notes
    appointment.doctor_name = doctor_name
    appointment.technician_name = technician_name
    appointment.usg_findings = usg_findings
    appointment.is_high_risk = is_high_risk or findings == "Abnormal"
    appointment.report_file_path = report_paths[0] if report_paths else None
    appointment.report_file_paths = json.dumps(report_paths) if report_paths else None
    appointment.completed_by = current_user.id

    # Write USG-confirmed EDD to PregnantWoman and append to EDD history
    from models import EDDHistory
    db.add(EDDHistory(
        pregnant_woman_id=pw.id,
        previous_edd=pw.edd_date,
        new_edd=confirmed_edd_parsed,
        source="USG_CONFIRMED",
        changed_by=current_user.id,
    ))
    pw.edd_date = confirmed_edd_parsed

    db.commit()
    db.refresh(appointment)
    
    # Send in-app notification to scheduler
    if appointment.scheduled_by and pw:
        NotificationService.create_notification(
            db=db,
            user_ids=[appointment.scheduled_by],
            title="USG Report Uploaded",
            message=f"USG report for {pw.full_name} has been uploaded and appointment is completed",
            notification_type="report_uploaded",
            category="appointment",
            priority="high" if is_high_risk else "normal",
            reference_id=appointment.id,
            reference_type="usg_appointment",
            action_url=f"/usg-appointments/{appointment.id}",
            metadata={"pregnant_woman_name": pw.full_name, "is_high_risk": is_high_risk}
        )
    
    # Schedule feedback call and send SMS
    from models import FeedbackRecord
    feedback = FeedbackRecord(
        pregnant_woman_id=appointment.pregnant_woman_id,
        usg_appointment_id=appointment.id,
        call_attempt=1,
        next_call_scheduled=datetime.now() + timedelta(hours=24)
    )
    db.add(feedback)
    db.commit()
    
    # Send feedback SMS notification
    if pw:
        _sms = get_sms_template("usg_completed_feedback")
        background_tasks.add_task(
            send_sms_notification,
            pw.mobile_number,
            _sms["message"],
            db,
            "usg_completed_feedback",
            pw.id,
            appointment.id,
            _sms["template_id"]
        )

    ip_address, user_agent = get_client_info(request)
    log_update(
        db,
        current_user.id,
        "USGAppointment",
        appointment.id,
        old_values,
        get_entity_snapshot(appointment),
        ip_address,
        user_agent
    )
    
    return format_appointment_response(appointment, db)

@router.post("/{appointment_id}/cancel")
async def cancel_appointment(
    appointment_id: int,
    cancellation_reason: str,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Cancel USG appointment (ANM/Sub-centre/Block users)
    """
    if current_user.role not in ["sub_centre", "block", "usg_centre", "pmsma"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Sub-centre, Block, USG centre, or PMSMA users can cancel appointments"
        )
    
    appointment = db.query(USGAppointment).filter(
        USGAppointment.id == appointment_id
    ).first()
    
    if not appointment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Appointment not found"
        )
    
    if appointment.status in ["completed", "cancelled"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel {appointment.status} appointment"
        )
    
    # Authorization check
    pw = db.query(PregnantWoman).filter(
        PregnantWoman.id == appointment.pregnant_woman_id
    ).first()
    
    if current_user.role == "sub_centre":
        if pw.sub_centre_id is not None and pw.sub_centre_id != current_user.sub_centre_id:
            if pw.block_id != current_user.block_id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and pw.block_id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "usg_centre" and appointment.usg_centre_id != current_user.usg_centre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "pmsma" and appointment.scheduled_by != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    # Update appointment
    old_values = get_entity_snapshot(appointment)
    appointment.status = "cancelled"
    appointment.reschedule_reason = f"Cancelled: {cancellation_reason}"
    appointment.updated_at = datetime.now()
    
    db.commit()
    
    # Send in-app notifications
    recipients = []
    if appointment.scheduled_by:
        recipients.append(appointment.scheduled_by)
    
    # Notify USG centre users
    usg_users = db.query(User).filter(
        User.usg_centre_id == appointment.usg_centre_id,
        User.is_active == True
    ).all()
    recipients.extend([u.id for u in usg_users])
    
    if recipients:
        NotificationService.create_notification(
            db=db,
            user_ids=list(set(recipients)),
            title="USG Appointment Cancelled",
            message=f"Appointment for {pw.full_name} has been cancelled. Reason: {cancellation_reason}",
            notification_type="appointment_cancelled",
            category="appointment",
            priority="normal",
            reference_id=appointment.id,
            reference_type="usg_appointment",
            action_url=f"/usg-appointments/{appointment.id}",
            metadata={"pregnant_woman_name": pw.full_name, "cancellation_reason": cancellation_reason}
        )
    
    # Send notifications
    # TODO: Uncomment when dedicated cancel SMS template is available
    # if pw:
    #     _sms = get_sms_template("usg_booking_confirmation",
    #         DATE=appointment.scheduled_date.strftime('%d-%m-%Y %H:%M'),
    #         USG_CENTRE=""
    #     )
    #     background_tasks.add_task(
    #         send_sms_notification,
    #         pw.mobile_number,
    #         _sms["message"],
    #         db,
    #         "usg_booking_confirmation",
    #         pw.id,
    #         appointment.id,
    #         _sms["template_id"]
    #     )

    ip_address, user_agent = get_client_info(request)
    log_update(
        db,
        current_user.id,
        "USGAppointment",
        appointment.id,
        old_values,
        get_entity_snapshot(appointment),
        ip_address,
        user_agent
    )
    
    return {
        "message": "Appointment cancelled successfully",
        "appointment_id": appointment.id,
        "cancellation_reason": cancellation_reason
    }

@router.get("/overdue/emergency")
async def get_overdue_emergency_appointments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get emergency appointments that are overdue (more than 7 days old)
    """
    seven_days_ago = datetime.now() - timedelta(days=7)
    
    query = db.query(USGAppointment).filter(
        USGAppointment.scheduled_date < seven_days_ago,
        USGAppointment.status.in_(["scheduled", "accepted"]),
        USGAppointment.appointment_type == "emergency"
    )
    
    # Role-based filtering (same as GET /)
    if current_user.role == "usg_centre":
        query = query.filter(USGAppointment.usg_centre_id == current_user.usg_centre_id)
    elif current_user.role == "block":
        query = query.join(PregnantWoman).filter(
            PregnantWoman.block_id == current_user.block_id
        )
    elif current_user.role == "sub_centre":
        from models import UserWardMapping, WardSubcentreMapping
        from sqlalchemy import or_
        user_wards = db.query(UserWardMapping.ward_id).filter(UserWardMapping.user_id == current_user.id).all()
        if user_wards:
            ward_ids = [w.ward_id for w in user_wards]
            query = query.join(PregnantWoman).filter(PregnantWoman.ward_id.in_(ward_ids))
        else:
            mapped_ward_ids = db.query(WardSubcentreMapping.ward_id).filter(
                WardSubcentreMapping.sub_centre_id == current_user.sub_centre_id
            ).subquery()
            query = query.join(PregnantWoman).filter(
                or_(PregnantWoman.ward_id.in_(mapped_ward_ids), PregnantWoman.sub_centre_id == current_user.sub_centre_id)
            )
    
    return [format_appointment_response(a, db) for a in query.all()]

@router.post("/notify-overdue")
async def notify_overdue_appointments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Send notifications for overdue appointments (scheduled job)
    """
    if current_user.role not in ["district", "block"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only district or block users can run this operation"
        )
    
    seven_days_ago = datetime.now() - timedelta(days=7)
    
    # Find overdue appointments
    query = db.query(USGAppointment).filter(
        USGAppointment.scheduled_date < seven_days_ago,
        USGAppointment.status.in_(["scheduled", "accepted"])
    )
    
    if current_user.role == "block":
        # Get appointments for pregnant women in this block
        query = query.join(PregnantWoman).filter(
            PregnantWoman.block_id == current_user.block_id
        )
    elif current_user.role == "district":
        query = query.join(PregnantWoman).filter(
            PregnantWoman.district_id == current_user.district_id
        )
    
    overdue_appointments = query.all()
    notified_count = 0
    
    for appointment in overdue_appointments:
        pw = db.query(PregnantWoman).filter(
            PregnantWoman.id == appointment.pregnant_woman_id
        ).first()
        
        if pw:
            # Notify district and block users
            recipients = NotificationService.get_recipients_for_event(
                db,
                "appointment_overdue",
                {"district_id": pw.district_id, "block_id": pw.block_id}
            )
            
            if recipients:
                NotificationService.create_notification(
                    db=db,
                    user_ids=recipients,
                    title="⚠️ Overdue USG Appointment",
                    message=f"Appointment for {pw.full_name} is overdue by {(datetime.now() - appointment.scheduled_date).days} days",
                    notification_type="appointment_overdue",
                    category="appointment",
                    priority="high",
                    reference_id=appointment.id,
                    reference_type="usg_appointment",
                    action_url=f"/usg-appointments/{appointment.id}",
                    metadata={
                        "pregnant_woman_name": pw.full_name,
                        "days_overdue": (datetime.now() - appointment.scheduled_date).days,
                        "scheduled_date": appointment.scheduled_date.isoformat()
                    }
                )
                notified_count += 1
    
    return {
        "message": f"Notified about {notified_count} overdue appointments",
        "count": notified_count
    }
