from fastapi import APIRouter, Depends, HTTPException, status, Request, UploadFile, File, Form
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date
import os
import uuid
import shutil
import logging

from database import get_db
from models import ECGReport, ECGResultEnum, PregnantWoman, DeliveryPoint, User
from auth import get_current_active_user
from audit_utils import get_client_info, get_entity_snapshot, log_create
from services.notification_service import NotificationService
from file_validator import validate_upload_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ecg-reports", tags=["ECG Reports"])

UPLOAD_DIR = "uploads/ecg_reports"


def _format_ecg(report: ECGReport, db: Session) -> dict:
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == report.pregnant_woman_id).first()
    dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == report.dp_id).first()
    user = db.query(User).filter(User.id == report.recorded_by_user_id).first()
    return {
        "id": report.id,
        "pregnant_woman_id": report.pregnant_woman_id,
        "pregnant_woman_name": pw.full_name if pw else None,
        "mobile_number": pw.mobile_number if pw else None,
        "dp_id": report.dp_id,
        "dp_name": dp.name if dp else None,
        "recorded_by_user_id": report.recorded_by_user_id,
        "recorded_by_name": user.full_name if user else None,
        "ecg_date": report.ecg_date,
        "result": report.result,
        "notes": report.notes,
        "report_file_url": f"/uploads/ecg_reports/{os.path.basename(report.report_file_path)}" if report.report_file_path else None,
        "created_at": report.created_at,
        "updated_at": report.updated_at,
    }


# ─── DP: Create ECG Report ───────────────────────────────────────────────────

@router.post("/", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_ecg_report(
    request: Request,
    pregnant_woman_id: int = Form(...),
    ecg_date: date = Form(...),
    result: str = Form(...),
    notes: Optional[str] = Form(None),
    report_file: Optional[UploadFile] = File(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """DP user creates an ECG report for a pregnant woman."""
    if current_user.role != "dp":
        raise HTTPException(status_code=403, detail="Only Delivery Point users can create ECG reports")

    if result not in ("normal", "abnormal"):
        raise HTTPException(status_code=422, detail="result must be 'normal' or 'abnormal'")

    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pregnant_woman_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")

    dp = db.query(DeliveryPoint).filter(
        DeliveryPoint.id == current_user.dp_id,
        DeliveryPoint.is_active == True
    ).first()
    if not dp:
        raise HTTPException(status_code=404, detail="Delivery Point not found or inactive")

    # Check duplicate: same pregnant woman + same ECG date
    existing = db.query(ECGReport).filter(
        ECGReport.pregnant_woman_id == pregnant_woman_id,
        ECGReport.ecg_date == ecg_date
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"An ECG report for {pw.full_name} on {ecg_date} already exists."
        )

    # Save file if provided
    file_path = None
    if report_file and report_file.filename:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        await validate_upload_file(report_file, "attachment")
        ext = os.path.splitext(report_file.filename)[1]
        file_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(report_file.file, buffer)

    ecg = ECGReport(
        pregnant_woman_id=pregnant_woman_id,
        dp_id=current_user.dp_id,
        recorded_by_user_id=current_user.id,
        ecg_date=ecg_date,
        result=ECGResultEnum(result),
        notes=notes,
        report_file_path=file_path,
    )
    db.add(ecg)
    db.commit()
    db.refresh(ecg)

    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "ECGReport", ecg.id, get_entity_snapshot(ecg), ip_address, user_agent)

    # ── Notifications ────────────────────────────────────────────────────────
    is_abnormal = result == "abnormal"
    priority = "high" if is_abnormal else "normal"
    title = "⚠️ Abnormal ECG Report" if is_abnormal else "ECG Report Created"
    message = (
        f"Abnormal ECG report recorded for {pw.full_name} at {dp.name}. Immediate attention required."
        if is_abnormal
        else f"ECG report recorded for {pw.full_name} at {dp.name}. Result: Normal."
    )

    recipient_ids = set()

    # Sub-centre users linked to the pregnant woman's sub_centre_id
    if pw.sub_centre_id:
        sc_users = db.query(User).filter(
            User.sub_centre_id == pw.sub_centre_id,
            User.role == "sub_centre",
            User.is_active == True
        ).all()
        recipient_ids.update(u.id for u in sc_users)

    # Block users linked to the pregnant woman's block_id
    if pw.block_id:
        block_users = db.query(User).filter(
            User.block_id == pw.block_id,
            User.role == "block",
            User.is_active == True
        ).all()
        recipient_ids.update(u.id for u in block_users)

    # District users linked to the pregnant woman's district_id
    if pw.district_id:
        district_users = db.query(User).filter(
            User.district_id == pw.district_id,
            User.role == "district",
            User.is_active == True
        ).all()
        recipient_ids.update(u.id for u in district_users)

    if recipient_ids:
        NotificationService.create_notification(
            db=db,
            user_ids=list(recipient_ids),
            title=title,
            message=message,
            notification_type="ecg_report_created",
            category="ecg",
            priority=priority,
            reference_id=ecg.id,
            reference_type="ecg_report",
            action_url="/ecg-report-management",
            metadata={"pregnant_woman_name": pw.full_name, "result": result, "dp_name": dp.name},
        )

    return {"message": "ECG report created successfully", "ecg_report_id": ecg.id}


# ─── List ECG Reports (role-scoped) ─────────────────────────────────────────

@router.get("/", response_model=List[dict])
async def get_ecg_reports(
    result: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List ECG reports scoped by role."""
    if current_user.role not in ("dp", "sub_centre", "block", "district"):
        raise HTTPException(status_code=403, detail="Not authorized")

    query = db.query(ECGReport)

    if current_user.role == "dp":
        query = query.filter(ECGReport.dp_id == current_user.dp_id)
    elif current_user.role == "sub_centre":
        pw_ids = db.query(PregnantWoman.id).filter(
            PregnantWoman.sub_centre_id == current_user.sub_centre_id
        ).subquery()
        query = query.filter(ECGReport.pregnant_woman_id.in_(pw_ids))
    elif current_user.role == "block":
        pw_ids = db.query(PregnantWoman.id).filter(
            PregnantWoman.block_id == current_user.block_id
        ).subquery()
        query = query.filter(ECGReport.pregnant_woman_id.in_(pw_ids))
    elif current_user.role == "district":
        pw_ids = db.query(PregnantWoman.id).filter(
            PregnantWoman.district_id == current_user.district_id
        ).subquery()
        query = query.filter(ECGReport.pregnant_woman_id.in_(pw_ids))

    if result:
        query = query.filter(ECGReport.result == result)
    if start_date:
        query = query.filter(ECGReport.ecg_date >= start_date)
    if end_date:
        query = query.filter(ECGReport.ecg_date <= end_date)

    reports = query.order_by(ECGReport.created_at.desc()).all()
    return [_format_ecg(r, db) for r in reports]


# ─── Get Single ECG Report ───────────────────────────────────────────────────

@router.get("/{ecg_id}", response_model=dict)
async def get_ecg_report(
    ecg_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if current_user.role not in ("dp", "sub_centre", "block", "district"):
        raise HTTPException(status_code=403, detail="Not authorized")

    ecg = db.query(ECGReport).filter(ECGReport.id == ecg_id).first()
    if not ecg:
        raise HTTPException(status_code=404, detail="ECG report not found")

    pw = db.query(PregnantWoman).filter(PregnantWoman.id == ecg.pregnant_woman_id).first()

    # Authorization check
    if current_user.role == "dp" and ecg.dp_id != current_user.dp_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "sub_centre" and pw and pw.sub_centre_id != current_user.sub_centre_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "block" and pw and pw.block_id != current_user.block_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "district" and pw and pw.district_id != current_user.district_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    return _format_ecg(ecg, db)


# ─── Get ECG Reports for a Pregnant Woman ───────────────────────────────────

@router.get("/pregnant-woman/{pw_id}", response_model=List[dict])
async def get_ecg_reports_for_woman(
    pw_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if current_user.role not in ("dp", "sub_centre", "block", "district"):
        raise HTTPException(status_code=403, detail="Not authorized")

    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pw_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")

    reports = db.query(ECGReport).filter(
        ECGReport.pregnant_woman_id == pw_id
    ).order_by(ECGReport.ecg_date.desc()).all()

    return [_format_ecg(r, db) for r in reports]
