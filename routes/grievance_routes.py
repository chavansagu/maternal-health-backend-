from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, BackgroundTasks, Request
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta
import uuid
import shutil
import os

from database import get_db
from models import Grievance, User, PregnantWoman
from schemas import GrievanceCreate, GrievanceResponse, GrievanceUpdate
from auth import get_current_active_user
from sms_service import sms_service
from sms_templates import get_sms_template
from file_validator import validate_upload_file
from audit_utils import get_client_info, get_entity_snapshot, log_create, log_update, log_bulk_action
from services.notification_service import NotificationService

def format_grievance_response(grievance):
    """Format grievance with file URL"""
    response_data = {
        "id": grievance.id,
        "ticket_number": grievance.ticket_number,
        "name": grievance.name,
        "mobile_number": grievance.mobile_number,
        "ward_id": grievance.ward_id,
        "block_id": grievance.block_id,
        "district_id": grievance.district_id,
        "grievance_note": grievance.grievance_note,
        "rch_id": grievance.rch_id,
        "status": grievance.status,
        "escalated_to_district": grievance.escalated_to_district,
        "created_at": grievance.created_at,
        "resolved_at": grievance.resolved_at,
        "resolution_note": grievance.resolution_note,
        "attachment_file_url": f"/uploads/grievances/{os.path.basename(grievance.attachment_path)}" if grievance.attachment_path else None
    }
    return response_data

router = APIRouter(prefix="/grievances", tags=["Grievance Management"])

def validate_mobile_number_format(mobile: str) -> bool:
    """Validate Indian mobile number format (10 digits, starts with 6-9)"""
    import re
    return bool(re.match(r'^[6-9]\d{9}$', mobile))

def check_mobile_registered(db: Session, mobile: str) -> dict:
    """Check if mobile number is registered in PregnantWoman table"""
    pw = db.query(PregnantWoman).filter(
        PregnantWoman.mobile_number == mobile,
        PregnantWoman.is_active == True
    ).first()
    
    if pw:
        return {
            "is_registered": True,
            "pregnant_woman_id": pw.id,
            "rch_id": pw.rch_id,
            "full_name": pw.full_name
        }
    else:
        return {
            "is_registered": False,
            "pregnant_woman_id": None,
            "rch_id": None,
            "full_name": None
        }

def generate_ticket_number():
    """Generate unique ticket number for grievance"""
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    random_suffix = str(uuid.uuid4())[:6].upper()
    return f"GRV-{timestamp}-{random_suffix}"

async def save_attachment(file: UploadFile) -> str:
    """Save grievance attachment file with validation"""
    # Validate file
    await validate_upload_file(file, "attachment")
    
    upload_dir = "uploads/grievances"
    os.makedirs(upload_dir, exist_ok=True)
    
    file_extension = os.path.splitext(file.filename)[1]
    unique_filename = f"{uuid.uuid4()}{file_extension}"
    file_path = os.path.join(upload_dir, unique_filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    return file_path

@router.post("/", response_model=GrievanceResponse, status_code=status.HTTP_201_CREATED)
async def submit_grievance(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    mobile_number: str = Form(...),
    grievance_note: str = Form(...),
    ward_id: Optional[int] = Form(None),
    block_id: int = Form(...),
    district_id: Optional[int] = Form(None),
    pregnant_woman_id: Optional[int] = Form(None),
    rch_id: Optional[str] = Form(None),
    attachment: Optional[UploadFile] = File(None)
):
    """
    Submit a new grievance (public endpoint - no authentication required for beneficiaries)
    Upload file directly using multipart/form-data
    """
    # VALIDATION 1: Check mobile number format
    if not validate_mobile_number_format(mobile_number):
        raise HTTPException(
            status_code=400,
            detail="Invalid mobile number format. Must be 10 digits starting with 6-9."
        )
    
    # VALIDATION 2: Check if mobile is registered as Pregnant Woman
    registration_check = check_mobile_registered(db, mobile_number)
    if not registration_check["is_registered"]:
        raise HTTPException(
            status_code=403,
            detail="You are not registered. Only registered women enrolled in the program can submit grievance."
        )
    
    # Auto-link to PregnantWoman if not already provided
    if not pregnant_woman_id:
        pregnant_woman_id = registration_check["pregnant_woman_id"]
    if not rch_id:
        rch_id = registration_check["rch_id"]
    
    # Save attachment if provided
    attachment_path = None
    if attachment and attachment.filename:
        attachment_path = await save_attachment(attachment)
    
    # Generate ticket number
    ticket_number = generate_ticket_number()
    
    # Determine district_id from block if not provided
    if not district_id:
        from models import Block
        block = db.query(Block).filter(Block.id == block_id).first()
        if block:
            district_id = block.district_id
    
    # Create grievance
    grievance = Grievance(
        ticket_number=ticket_number,
        name=name,
        mobile_number=mobile_number,
        rch_id=rch_id,
        ward_id=ward_id,
        block_id=block_id,
        district_id=district_id,
        pregnant_woman_id=pregnant_woman_id,
        grievance_note=grievance_note,
        attachment_path=attachment_path,
        assigned_to_block=block_id,
        status="pending"
    )
    
    db.add(grievance)
    db.commit()
    db.refresh(grievance)

    ip_address, user_agent = get_client_info(request)
    log_create(
        db,
        None,
        "Grievance",
        grievance.id,
        get_entity_snapshot(grievance),
        ip_address,
        user_agent
    )
    
    # Send in-app notification to block users
    recipients = NotificationService.get_recipients_for_event(
        db,
        "grievance_new",
        {"block_id": block_id}
    )
    
    if recipients:
        NotificationService.create_notification(
            db=db,
            user_ids=recipients,
            title="New Grievance Submitted",
            message=f"Grievance {ticket_number} submitted by {name}",
            notification_type="grievance_new",
            category="grievance",
            priority="normal",
            reference_id=grievance.id,
            reference_type="grievance",
            action_url=f"/grievances/{grievance.id}",
            metadata={"ticket_number": ticket_number, "submitter_name": name}
        )
    
    # Create notification for block users (old method - keeping for backward compatibility)
    from models import Notification
    if block_id:
        block_users = db.query(User).filter(
            User.block_id == block_id,
            User.role == "block",
            User.is_active == True
        ).all()
        
        for block_user in block_users:
            notification = Notification(
                user_id=block_user.id,
                title="New Grievance Submitted",
                message=f"Grievance {ticket_number} submitted by {name}",
                notification_type="grievance",
                reference_id=grievance.id,
                reference_type="grievance"
            )
            db.add(notification)
        
        db.commit()
    
    # Send grievance acknowledgement SMS
    _sms = get_sms_template("grievance_acknowledgement")
    background_tasks.add_task(
        sms_service.send_sms,
        mobile_number=mobile_number,
        message=_sms["message"],
        message_type="grievance_acknowledgement",
        db=db,
        template_id=_sms["template_id"]
    )
    
    return format_grievance_response(grievance)

@router.get("/", response_model=List[GrievanceResponse])
async def get_grievances(
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = None,
    block_id: Optional[int] = None,
    district_id: Optional[int] = None,
    escalated_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get list of grievances with filters
    """
    query = db.query(Grievance)
    
    # Role-based filtering
    if current_user.role == "district":
        query = query.filter(Grievance.district_id == current_user.district_id)
        if escalated_only:
            query = query.filter(Grievance.escalated_to_district == True)
    elif current_user.role == "block":
        query = query.filter(Grievance.assigned_to_block == current_user.block_id)
    
    # Additional filters
    if status:
        query = query.filter(Grievance.status == status)
    if block_id:
        query = query.filter(Grievance.block_id == block_id)
    if district_id:
        query = query.filter(Grievance.district_id == district_id)
    
    grievances = query.order_by(Grievance.created_at.desc()).offset(skip).limit(limit).all()
    return [format_grievance_response(g) for g in grievances]

@router.get("/pending", response_model=List[GrievanceResponse])
async def get_pending_grievances(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get pending grievances for current user's jurisdiction
    """
    query = db.query(Grievance).filter(Grievance.status == "pending")
    
    if current_user.role == "block":
        query = query.filter(Grievance.assigned_to_block == current_user.block_id)
    elif current_user.role == "district":
        query = query.filter(
            Grievance.district_id == current_user.district_id,
            Grievance.escalated_to_district == True
        )
    
    return [format_grievance_response(g) for g in query.all()]

@router.get("/overdue", response_model=List[GrievanceResponse])
async def get_overdue_grievances(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get grievances that are overdue (more than 7 days old and not resolved)
    """
    seven_days_ago = datetime.now() - timedelta(days=7)
    
    query = db.query(Grievance).filter(
        Grievance.created_at < seven_days_ago,
        Grievance.status != "resolved"
    )
    
    if current_user.role == "block":
        query = query.filter(Grievance.assigned_to_block == current_user.block_id)
    elif current_user.role == "district":
        query = query.filter(Grievance.district_id == current_user.district_id)
    
    return [format_grievance_response(g) for g in query.all()]

@router.post("/notify-overdue")
async def notify_overdue_grievances(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Send notifications for overdue grievances (scheduled job)
    """
    if current_user.role not in ["district", "block"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only district or block users can run this operation"
        )
    
    seven_days_ago = datetime.now() - timedelta(days=7)
    
    # Find overdue grievances
    query = db.query(Grievance).filter(
        Grievance.created_at < seven_days_ago,
        Grievance.status.in_(["pending", "in_progress"])
    )
    
    if current_user.role == "block":
        query = query.filter(Grievance.assigned_to_block == current_user.block_id)
    elif current_user.role == "district":
        query = query.filter(Grievance.district_id == current_user.district_id)
    
    overdue_grievances = query.all()
    notified_count = 0
    
    for grievance in overdue_grievances:
        # Notify district and block users
        recipients = NotificationService.get_recipients_for_event(
            db,
            "grievance_overdue",
            {"district_id": grievance.district_id, "block_id": grievance.block_id}
        )
        
        if recipients:
            NotificationService.create_notification(
                db=db,
                user_ids=recipients,
                title="⚠️ Overdue Grievance",
                message=f"Grievance {grievance.ticket_number} is overdue by {(datetime.now() - grievance.created_at).days} days",
                notification_type="grievance_overdue",
                category="grievance",
                priority="high",
                reference_id=grievance.id,
                reference_type="grievance",
                action_url=f"/grievances/{grievance.id}",
                metadata={
                    "ticket_number": grievance.ticket_number,
                    "days_overdue": (datetime.now() - grievance.created_at).days
                }
            )
            notified_count += 1
    
    return {
        "message": f"Notified about {notified_count} overdue grievances",
        "count": notified_count
    }

@router.get("/track/{ticket_number}", response_model=GrievanceResponse)
async def track_grievance(
    ticket_number: str,
    db: Session = Depends(get_db)
):
    """
    Track grievance by ticket number (public endpoint)
    """
    grievance = db.query(Grievance).filter(
        Grievance.ticket_number == ticket_number
    ).first()
    
    if not grievance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Grievance not found"
        )
    
    return format_grievance_response(grievance)

@router.get("/{grievance_id}", response_model=GrievanceResponse)
async def get_grievance(
    grievance_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get grievance details by ID
    """
    grievance = db.query(Grievance).filter(Grievance.id == grievance_id).first()
    
    if not grievance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Grievance not found"
        )
    
    # Authorization check
    if current_user.role == "block" and grievance.assigned_to_block != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "district" and grievance.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    return format_grievance_response(grievance)

@router.put("/{grievance_id}", response_model=GrievanceResponse)
async def update_grievance_status(
    grievance_id: int,
    grievance_update: GrievanceUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Update grievance status and add resolution notes
    """
    grievance = db.query(Grievance).filter(Grievance.id == grievance_id).first()
    
    if not grievance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Grievance not found"
        )
    
    # Authorization check
    if current_user.role == "block":
        if grievance.assigned_to_block != current_user.block_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "district":
        if grievance.district_id != current_user.district_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Update fields
    old_values = get_entity_snapshot(grievance)
    if grievance_update.status:
        old_status = grievance.status
        grievance.status = grievance_update.status
        
        if grievance_update.status == "resolved":
            grievance.resolved_at = datetime.now()
            grievance.resolved_by = current_user.id
            
            # Notify district users about resolution
            if grievance.district_id:
                district_users = db.query(User).filter(
                    User.district_id == grievance.district_id,
                    User.role == "district",
                    User.is_active == True
                ).all()
                
                if district_users:
                    NotificationService.create_notification(
                        db=db,
                        user_ids=[u.id for u in district_users],
                        title="Grievance Resolved",
                        message=f"Grievance {grievance.ticket_number} has been resolved",
                        notification_type="grievance_resolved",
                        category="grievance",
                        priority="normal",
                        reference_id=grievance.id,
                        reference_type="grievance",
                        action_url=f"/grievances/{grievance.id}",
                        metadata={"ticket_number": grievance.ticket_number}
                    )
        
        elif grievance_update.status == "in_progress" and old_status == "pending":
            # Notify about status change to in_progress
            if grievance.district_id:
                district_users = db.query(User).filter(
                    User.district_id == grievance.district_id,
                    User.role == "district",
                    User.is_active == True
                ).all()
                
                if district_users:
                    NotificationService.create_notification(
                        db=db,
                        user_ids=[u.id for u in district_users],
                        title="Grievance In Progress",
                        message=f"Grievance {grievance.ticket_number} is now being processed",
                        notification_type="grievance_in_progress",
                        category="grievance",
                        priority="normal",
                        reference_id=grievance.id,
                        reference_type="grievance",
                        action_url=f"/grievances/{grievance.id}",
                        metadata={"ticket_number": grievance.ticket_number}
                    )
    
    if grievance_update.resolution_note:
        grievance.resolution_note = grievance_update.resolution_note
    
    grievance.assigned_to_user = current_user.id
    
    # Send notification if grievance is being assigned
    if not old_values.get("assigned_to_user") and grievance.assigned_to_user:
        NotificationService.create_notification(
            db=db,
            user_ids=[current_user.id],
            title="Grievance Assigned to You",
            message=f"Grievance {grievance.ticket_number} has been assigned to you",
            notification_type="grievance_assigned",
            category="grievance",
            priority="normal",
            reference_id=grievance.id,
            reference_type="grievance",
            action_url=f"/grievances/{grievance.id}",
            metadata={"ticket_number": grievance.ticket_number}
        )
    db.commit()
    db.refresh(grievance)

    ip_address, user_agent = get_client_info(request)
    log_update(
        db,
        current_user.id,
        "Grievance",
        grievance.id,
        old_values,
        get_entity_snapshot(grievance),
        ip_address,
        user_agent
    )
    
    # Send notification if status changed to resolved
    if grievance_update.status == "resolved":
        # Notify district users about resolution
        if grievance.district_id:
            district_users = db.query(User).filter(
                User.district_id == grievance.district_id,
                User.role == "district",
                User.is_active == True
            ).all()
            
            if district_users:
                NotificationService.create_notification(
                    db=db,
                    user_ids=[u.id for u in district_users],
                    title="Grievance Resolved",
                    message=f"Grievance {grievance.ticket_number} has been resolved",
                    notification_type="grievance_resolved",
                    category="grievance",
                    priority="normal",
                    reference_id=grievance.id,
                    reference_type="grievance",
                    action_url=f"/grievances/{grievance.id}",
                    metadata={"ticket_number": grievance.ticket_number}
                )
    
    return format_grievance_response(grievance)

@router.post("/{grievance_id}/resolve")
async def resolve_grievance(
    grievance_id: int,
    resolution_note: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Mark grievance as resolved
    """
    grievance = db.query(Grievance).filter(Grievance.id == grievance_id).first()
    
    if not grievance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Grievance not found"
        )
    
    old_values = get_entity_snapshot(grievance)
    grievance.status = "resolved"
    grievance.resolution_note = resolution_note
    grievance.resolved_by = current_user.id
    grievance.resolved_at = datetime.now()
    
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_update(
        db,
        current_user.id,
        "Grievance",
        grievance.id,
        old_values,
        get_entity_snapshot(grievance),
        ip_address,
        user_agent
    )
    
    return {"message": "Grievance resolved successfully", "ticket_number": grievance.ticket_number}

@router.post("/auto-escalate")
async def auto_escalate_overdue_grievances(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Auto-escalate grievances that are more than 7 days old (manual trigger by district user)
    """
    if current_user.role != "district":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only district users can run this operation"
        )
    
    seven_days_ago = datetime.now() - timedelta(days=7)
    
    # Find overdue grievances
    overdue_grievances = db.query(Grievance).filter(
        Grievance.created_at < seven_days_ago,
        Grievance.status.in_(["pending", "in_progress"]),
        Grievance.escalated_to_district == False,
        Grievance.district_id == current_user.district_id
    ).all()
    
    escalated_count = 0
    for grievance in overdue_grievances:
        grievance.escalated_to_district = True
        grievance.auto_escalated = True
        grievance.escalation_date = datetime.now()
        escalated_count += 1
        
        # Send in-app notification to district users
        recipients = NotificationService.get_recipients_for_event(
            db,
            "grievance_escalated",
            {"district_id": current_user.district_id}
        )
        
        if recipients:
            NotificationService.create_notification(
                db=db,
                user_ids=recipients,
                title="Grievance Auto-Escalated",
                message=f"Grievance {grievance.ticket_number} has been auto-escalated due to pending resolution",
                notification_type="grievance_escalated",
                category="grievance",
                priority="high",
                reference_id=grievance.id,
                reference_type="grievance",
                action_url=f"/grievances/{grievance.id}",
                metadata={"ticket_number": grievance.ticket_number, "auto_escalated": True}
            )
        
        # Create notification for district users (old method - keeping for backward compatibility)
        from models import Notification
        district_users = db.query(User).filter(
            User.district_id == current_user.district_id,
            User.role == "district",
            User.is_active == True
        ).all()
        
        for district_user in district_users:
            notification = Notification(
                user_id=district_user.id,
                title="Grievance Auto-Escalated",
                message=f"Grievance {grievance.ticket_number} has been auto-escalated due to pending resolution",
                notification_type="grievance_escalation",
                reference_id=grievance.id,
                reference_type="grievance"
            )
            db.add(notification)
    
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_bulk_action(
        db,
        current_user.id,
        "AUTO_ESCALATE",
        "Grievance",
        escalated_count,
        {"district_id": current_user.district_id, "escalated_count": escalated_count},
        ip_address,
        user_agent
    )
    
    return {
        "message": f"Auto-escalated {escalated_count} grievances",
        "count": escalated_count
    }

@router.post("/cron/auto-escalate")
async def cron_auto_escalate_grievances(
    request: Request,
    db: Session = Depends(get_db),
    cron_secret: str = Form(...)
):
    """
    Auto-escalate grievances - Called by cron job (no user authentication)
    Requires CRON_SECRET from .env for security
    """
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    # Security check - verify cron secret
    expected_secret = os.getenv("CRON_SECRET", "change-this-secret-in-production")
    if cron_secret != expected_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid cron secret"
        )
    
    seven_days_ago = datetime.now() - timedelta(days=7)
    
    # Find ALL overdue grievances across all districts
    overdue_grievances = db.query(Grievance).filter(
        Grievance.created_at < seven_days_ago,
        Grievance.status.in_(["pending", "in_progress"]),
        Grievance.escalated_to_district == False
    ).all()
    
    escalated_count = 0
    escalated_by_district = {}
    
    for grievance in overdue_grievances:
        grievance.escalated_to_district = True
        grievance.auto_escalated = True
        grievance.escalation_date = datetime.now()
        grievance.status = "escalated"  # Change status to escalated
        escalated_count += 1
        
        # Track count by district
        district_id = grievance.district_id
        if district_id not in escalated_by_district:
            escalated_by_district[district_id] = 0
        escalated_by_district[district_id] += 1
        
        # Send in-app notification to district users
        if district_id:
            recipients = NotificationService.get_recipients_for_event(
                db,
                "grievance_escalated",
                {"district_id": district_id}
            )
            
            if recipients:
                NotificationService.create_notification(
                    db=db,
                    user_ids=recipients,
                    title="🚨 Grievance Auto-Escalated",
                    message=f"Grievance {grievance.ticket_number} has been auto-escalated after 7 days",
                    notification_type="grievance_escalated",
                    category="grievance",
                    priority="high",
                    reference_id=grievance.id,
                    reference_type="grievance",
                    action_url=f"/grievances/{grievance.id}",
                    metadata={
                        "ticket_number": grievance.ticket_number,
                        "auto_escalated": True,
                        "days_pending": (datetime.now() - grievance.created_at).days
                    }
                )
    
    db.commit()
    
    # Log the bulk action
    ip_address, user_agent = get_client_info(request)
    log_bulk_action(
        db,
        None,  # No user, it's a cron job
        "CRON_AUTO_ESCALATE",
        "Grievance",
        escalated_count,
        {
            "total_escalated": escalated_count,
            "by_district": escalated_by_district,
            "triggered_by": "cron_job"
        },
        ip_address,
        user_agent
    )
    
    return {
        "success": True,
        "message": f"Auto-escalated {escalated_count} grievances across all districts",
        "total_count": escalated_count,
        "by_district": escalated_by_district,
        "timestamp": datetime.now().isoformat()
    }

@router.get("/statistics/summary")
async def get_grievance_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get grievance statistics summary
    """
    from sqlalchemy import func
    
    query = db.query(Grievance)
    
    if current_user.role == "block":
        query = query.filter(Grievance.assigned_to_block == current_user.block_id)
    elif current_user.role == "district":
        query = query.filter(Grievance.district_id == current_user.district_id)
    
    total = query.count()
    pending = query.filter(Grievance.status == "pending").count()
    in_progress = query.filter(Grievance.status == "in_progress").count()
    resolved = query.filter(Grievance.status == "resolved").count()
    escalated = query.filter(Grievance.escalated_to_district == True).count()
    
    # Average resolution time
    resolved_grievances = query.filter(
        Grievance.status == "resolved",
        Grievance.resolved_at.isnot(None)
    ).all()
    
    avg_resolution_time = 0
    if resolved_grievances:
        total_time = sum([
            (g.resolved_at - g.created_at).days 
            for g in resolved_grievances
        ])
        avg_resolution_time = total_time / len(resolved_grievances)
    
    return {
        "total_grievances": total,
        "pending": pending,
        "in_progress": in_progress,
        "resolved": resolved,
        "escalated": escalated,
        "avg_resolution_time_days": round(avg_resolution_time, 2)
    }
