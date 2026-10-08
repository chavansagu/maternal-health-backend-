from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, date, timedelta
import logging

from database import get_db
from models import ANCVisit, PregnantWoman, User, Notification
from schemas import ANCVisitCreate, ANCVisitResponse, ANCVisitUpdate
from auth import get_current_active_user
from audit_utils import get_client_info, get_entity_snapshot, log_create, log_update, log_delete
from services.notification_service import NotificationService
from sms_service import sms_service
from sms_templates import get_sms_template
from ivr_service import trigger_high_risk_call

from services.tracking_service import get_tracking_info
from sqlalchemy import func
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/anc-visits", tags=["ANC Visits"])


def evaluate_high_risk(visit: ANCVisit) -> List[str]:
    """
    Evaluate ANC visit clinical values against high-risk thresholds.
    Returns a list of triggered risk reasons. Empty list = no risk detected.

    Thresholds:
      Blood Pressure : systolic >= 140 OR diastolic >= 90  (hypertension)
                       systolic < 100  OR diastolic < 70   (hypotension)
      Hemoglobin     : < 7 g/dL  (severe anaemia)  OR  > 15 g/dL (polycythaemia)
      Weight         : >= 80 kg
      Fetal Heart Rate: < 110 bpm
    Missing / None values are skipped (not treated as risk).
    """
    reasons = []

    # --- Blood Pressure ---
    if visit.blood_pressure:
        try:
            parts = str(visit.blood_pressure).strip().split("/")
            if len(parts) == 2:
                systolic = float(parts[0].strip())
                diastolic = float(parts[1].strip())
                if systolic >= 140 or diastolic >= 90:
                    reasons.append(f"High BP ({visit.blood_pressure})")
                elif systolic < 100 or diastolic < 70:
                    reasons.append(f"Low BP ({visit.blood_pressure})")
        except (ValueError, AttributeError):
            pass  # unparseable BP string — skip silently

    # --- Hemoglobin ---
    if visit.hemoglobin is not None:
        if visit.hemoglobin < 7:
            reasons.append(f"Low Hemoglobin ({visit.hemoglobin} g/dL)")
        elif visit.hemoglobin > 15:
            reasons.append(f"High Hemoglobin ({visit.hemoglobin} g/dL)")

    # --- Weight ---
    if visit.weight is not None:
        if visit.weight >= 80:
            reasons.append(f"High Weight ({visit.weight} kg)")

    # --- Fetal Heart Rate ---
    if visit.fetal_heart_rate is not None:
        if visit.fetal_heart_rate < 110:
            reasons.append(f"Low Fetal Heart Rate ({visit.fetal_heart_rate} bpm)")

    return reasons

def get_tracking_info(pw: PregnantWoman) -> dict:
    """
    Gestational age is calculated from LMP up to today.
    28+ weeks and still active -> 'enhanced', otherwise 'regular'.
    """
    weeks = None
    if pw and pw.lmp_date:
        lmp = pw.lmp_date.date() if isinstance(pw.lmp_date, datetime) else pw.lmp_date
        weeks = (date.today() - lmp).days // 7

    is_delivered = bool(pw) and (not pw.is_active or pw.pregnancy_outcome is not None)
    tracking_type = "enhanced" if (weeks is not None and 28 <= weeks <= 42 and not is_delivered) else "regular"

    return {
        "gestational_weeks": weeks,
        "tracking_type": tracking_type,
    } 

def _apply_high_risk_flag(db, pw: PregnantWoman, risk_reasons: List[str], current_user: User):
    """
    Set is_high_risk=True on the PregnantWoman record, update risk_factors,
    fire in-app notifications, SMS, and IVR call. Only acts when pw is not already high-risk.
    """
    from models import IVRCallLog
    pw.is_high_risk = True
    pw.risk_factors = "Auto-detected: " + ", ".join(risk_reasons)
    db.commit()

    # In-app notification to district + block + sub-centre users
    recipients = NotificationService.get_recipients_for_event(
        db,
        "high_risk_alert",
        {
            "district_id": pw.district_id,
            "block_id": pw.block_id,
            "sub_centre_id": pw.sub_centre_id,
        }
    )
    if recipients:
        NotificationService.create_notification(
            db=db,
            user_ids=recipients,
            title="⚠️ High-Risk Case Auto-Detected",
            message=(
                f"{pw.full_name} has been automatically flagged as high-risk "
                f"based on ANC visit values. Reasons: {', '.join(risk_reasons)}"
            ),
            notification_type="high_risk_alert",
            category="anc_visit",
            priority="high",
            reference_id=pw.id,
            reference_type="pregnant_woman",
            action_url=f"/pregnant-women/{pw.id}",
            metadata={"pregnant_woman_name": pw.full_name, "risk_factors": pw.risk_factors}
        )

    # SMS to beneficiary
    try:
        _sms = get_sms_template("high_risk_alert")
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(
                sms_service.send_sms(
                    mobile_number=pw.mobile_number,
                    message=_sms["message"],
                    message_type="high_risk_alert",
                    pregnant_woman_id=pw.id,
                    db=db,
                    template_id=_sms["template_id"]
                )
            )
        else:
            loop.run_until_complete(
                sms_service.send_sms(
                    mobile_number=pw.mobile_number,
                    message=_sms["message"],
                    message_type="high_risk_alert",
                    pregnant_woman_id=pw.id,
                    db=db,
                    template_id=_sms["template_id"]
                )
            )
    except Exception:
        pass  # SMS is best-effort — never block the main flow

    # IVR advisory call to beneficiary
    try:
        ivr_result = trigger_high_risk_call(pw.mobile_number, pw.id)
        call_log = IVRCallLog(
            pregnant_woman_id=pw.id,
            mobile_number=pw.mobile_number,
            call_type="high_risk_advisory",
            call_status="initiated" if ivr_result["success"] else "failed",
            ivr_campaign_id=ivr_result.get("campaign_id"),
            provider_response=ivr_result.get("raw_response"),
            called_at=datetime.now() if ivr_result["success"] else None,
        )
        db.add(call_log)
        db.commit()
        logger.info(
            f"[IVR AUTO] pw_id={pw.id} name='{pw.full_name}' "
            f"success={ivr_result['success']} campaign_id={ivr_result.get('campaign_id')}"
        )
    except Exception:
        pass  # IVR is best-effort — never block the main flow

def send_overdue_notifications(db: Session) -> dict:
    """Overdue ANC visit -> notify ONLY the PW's own sub-centre users."""
    today = date.today()

    visits = db.query(ANCVisit).filter(
        ANCVisit.next_visit_date.isnot(None),
        ANCVisit.next_visit_date < today,
    ).all()

    notified = 0
    skipped = 0

    for visit in visits:
        # Only the latest visit of the PW counts
        newer = db.query(ANCVisit).filter(
            ANCVisit.pregnant_woman_id == visit.pregnant_woman_id,
            ANCVisit.visit_number > visit.visit_number,
        ).first()
        if newer:
            continue

        pw = db.query(PregnantWoman).filter(
            PregnantWoman.id == visit.pregnant_woman_id
        ).first()
        if not pw or not pw.is_active or pw.pregnancy_outcome is not None:
            continue
        if not pw.sub_centre_id:
            skipped += 1
            continue

        # Only this PW's own sub-centre users
        sc_users = db.query(User).filter(
            User.sub_centre_id == pw.sub_centre_id,
            User.role == "sub_centre",
            User.is_active == True,
        ).all()
        if not sc_users:
            skipped += 1
            continue

        # Don't send the same alert twice on the same day
        already = db.query(Notification).filter(
            Notification.notification_type == "anc_visit_overdue",
            Notification.reference_id == visit.id,
            Notification.reference_type == "anc_visit",
            func.date(Notification.created_at) == today,
        ).first()
        if already:
            continue

        days_overdue = (today - visit.next_visit_date).days
        NotificationService.create_notification(
            db=db,
            user_ids=[u.id for u in sc_users],
            title="ANC Visit Overdue",
            message=(
                f"{pw.full_name}'s ANC visit was due on "
                f"{visit.next_visit_date.strftime('%d/%m/%Y')} "
                f"({days_overdue} day(s) overdue)"
            ),
            notification_type="anc_visit_overdue",
            category="anc_visit",
            priority="high",
            reference_id=visit.id,
            reference_type="anc_visit",
            action_url=f"/pregnant-women/{pw.id}",
            metadata={
                "pregnant_woman_name": pw.full_name,
                "days_overdue": days_overdue,
            },
        )
        notified += 1

    return {"notified": notified, "skipped_no_subcentre_or_user": skipped}
    
@router.post("/", response_model=ANCVisitResponse, status_code=status.HTTP_201_CREATED)
async def create_anc_visit(
    visit_data: ANCVisitCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Create new ANC visit record (Sub-Centre/Block users)"""
    if current_user.role not in ["sub_centre", "block"]:
        raise HTTPException(status_code=403, detail="Only Sub-Centre or Block users can create ANC visits")
    
    # Verify pregnant woman exists
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == visit_data.pregnant_woman_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")

    # Block ANC creation if delivery already completed
    if not pw.is_active or pw.pregnancy_outcome is not None:
        logger.warning(f"Attempt to create ANC visit for inactive/delivered PW: {pw.id}")
        raise HTTPException(
            status_code=400,
            detail=f"Cannot create ANC visit. Beneficiary '{pw.full_name}' has already completed delivery (outcome: {pw.pregnancy_outcome or 'inactive'}). ANC visits are only allowed for active pregnancies."
        )

    # Authorization check
    if current_user.role == "sub_centre":
        # Allow if woman belongs to same sub-centre, OR sub_centre_id is unset (self-registered),
        # OR woman is in the same block as the user's sub-centre
        if pw.sub_centre_id is not None and pw.sub_centre_id != current_user.sub_centre_id:
            if pw.block_id != current_user.block_id:
                raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "block" and pw.block_id != current_user.block_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Create ANC visit
    new_visit = ANCVisit(
        **visit_data.dict(),
        attended_by=current_user.id
    )
    
    db.add(new_visit)
    db.commit()
    db.refresh(new_visit)

    ip_address, user_agent = get_client_info(request)
    log_create(
        db,
        current_user.id,
        "ANCVisit",
        new_visit.id,
        get_entity_snapshot(new_visit),
        ip_address,
        user_agent
    )

    # Send ANC visit recorded notification
    NotificationService.create_notification(
        db=db,
        user_ids=[current_user.id],
        title="ANC Visit Recorded",
        message=f"ANC Visit #{visit_data.visit_number} for {pw.full_name} has been recorded successfully",
        notification_type="anc_visit_recorded",
        category="anc_visit",
        priority="normal",
        reference_id=new_visit.id,
        reference_type="anc_visit",
        action_url=f"/anc-visits/{new_visit.id}",
        metadata={"pregnant_woman_name": pw.full_name, "visit_number": visit_data.visit_number}
    )

    # If referred for PMSMA, create notification
    if visit_data.referred_for_usg:
        block_users = db.query(User).filter(
            User.block_id == pw.block_id,
            User.role == "block",
            User.is_active == True
        ).all()

        if block_users:
            NotificationService.create_notification(
                db=db,
                user_ids=[u.id for u in block_users],
                title="PMSMA Referral from ANC Visit",
                message=f"ANC Visit #{visit_data.visit_number} for {pw.full_name} has been referred for PMSMA",
                notification_type="usg_referral",
                category="anc_visit",
                priority="high" if visit_data.is_emergency else "normal",
                reference_id=new_visit.id,
                reference_type="anc_visit",
                action_url=f"/anc-visits/{new_visit.id}",
                metadata={"pregnant_woman_name": pw.full_name, "visit_number": visit_data.visit_number, "is_emergency": visit_data.is_emergency}
            )

        # Old notification method (keeping for backward compatibility)
        for user in block_users:
            notification = Notification(
                user_id=user.id,
                title="PMSMA Referral from ANC Visit",
                message=f"ANC Visit #{visit_data.visit_number} for {pw.full_name} has been referred for PMSMA",
                notification_type="usg_referral",
                reference_id=new_visit.id,
                reference_type="anc_visit"
            )
            db.add(notification)
        db.commit()

    # --- Auto High-Risk Detection ---
    high_risk_triggered = False
    risk_reasons = evaluate_high_risk(new_visit)

    if risk_reasons and not pw.is_high_risk:
        _apply_high_risk_flag(db, pw, risk_reasons, current_user)
        high_risk_triggered = True
        logger.info(
            f"[HIGH-RISK AUTO] pw_id={pw.id} name='{pw.full_name}' "
            f"visit_id={new_visit.id} reasons={risk_reasons}"
        )
    elif visit_data.is_emergency and not pw.is_high_risk:
        # Fallback: emergency flag still marks high-risk even if vitals are normal
        pw.is_high_risk = True
        db.commit()

    return {
        "id": new_visit.id,
        "pregnant_woman_id": new_visit.pregnant_woman_id,
        "visit_number": new_visit.visit_number,
        "visit_date": new_visit.visit_date,
        "weight": new_visit.weight,
        "blood_pressure": new_visit.blood_pressure,
        "hemoglobin": new_visit.hemoglobin,
        "urine_albumin": new_visit.urine_albumin,   # NEW
        "urine_sugar": new_visit.urine_sugar,
        "fundal_height": new_visit.fundal_height,
        "fetal_heart_rate": new_visit.fetal_heart_rate,
        "referred_for_usg": new_visit.referred_for_usg,
        "is_emergency": new_visit.is_emergency,
        "doctor_notes": new_visit.doctor_notes,
        "next_visit_date": new_visit.next_visit_date,
        "facility_name": new_visit.facility_name,
        "attended_by": new_visit.attended_by,
        "created_at": new_visit.created_at,
        "updated_at": new_visit.updated_at,
        "high_risk_triggered": high_risk_triggered,
        "risk_reasons": risk_reasons,
    }

@router.get("/", response_model=List[dict])  # noqa: keep dict response
async def get_anc_visits(
    skip: int = 0,
    limit: int = 100,
    pregnant_woman_id: Optional[int] = None,
    visit_number: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    referred_for_usg: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get ANC visits with filters (role-based access)"""
    query = db.query(ANCVisit)
    
    # Role-based filtering — must match the same logic as GET /pregnant-women/
    if current_user.role == "district":
        query = query.join(PregnantWoman).filter(PregnantWoman.district_id == current_user.district_id)
    elif current_user.role == "block":
        query = query.join(PregnantWoman).filter(PregnantWoman.block_id == current_user.block_id)
    elif current_user.role == "sub_centre":
        from models import UserWardMapping, WardSubcentreMapping
        from sqlalchemy import or_
        user_wards = db.query(UserWardMapping.ward_id).filter(
            UserWardMapping.user_id == current_user.id
        ).all()
        if user_wards:
            ward_ids = [w.ward_id for w in user_wards]
            query = query.join(PregnantWoman).filter(PregnantWoman.ward_id.in_(ward_ids))
        else:
            mapped_ward_ids = db.query(WardSubcentreMapping.ward_id).filter(
                WardSubcentreMapping.sub_centre_id == current_user.sub_centre_id
            ).subquery()
            query = query.join(PregnantWoman).filter(
                or_(
                    PregnantWoman.ward_id.in_(mapped_ward_ids),
                    PregnantWoman.sub_centre_id == current_user.sub_centre_id
                )
            )
    elif current_user.role == "usg_centre":
        query = query.filter(ANCVisit.referred_for_usg == True)
    
    # Additional filters
    if pregnant_woman_id:
        query = query.filter(ANCVisit.pregnant_woman_id == pregnant_woman_id)
    if visit_number:
        query = query.filter(ANCVisit.visit_number == visit_number)
    if start_date:
        query = query.filter(ANCVisit.visit_date >= start_date)
    if end_date:
        query = query.filter(ANCVisit.visit_date <= end_date)
    if referred_for_usg is not None:
        query = query.filter(ANCVisit.referred_for_usg == referred_for_usg)
    
    visits = query.order_by(ANCVisit.visit_date.desc()).offset(skip).limit(limit).all()

    result = []
    for visit in visits:
        pw = db.query(PregnantWoman).filter(PregnantWoman.id == visit.pregnant_woman_id).first()
        tracking = get_tracking_info(pw)
        result.append({
            "id": visit.id,
            "pregnant_woman_id": visit.pregnant_woman_id,
            "pregnant_woman_name": pw.full_name if pw else None,
            "visit_number": visit.visit_number,
            "visit_date": visit.visit_date,
            "weight": visit.weight,
            "blood_pressure": visit.blood_pressure,
            "hemoglobin": visit.hemoglobin,
            "urine_albumin": visit.urine_albumin,   # NEW
            "urine_sugar": visit.urine_sugar,
            "fundal_height": visit.fundal_height,
            "fetal_heart_rate": visit.fetal_heart_rate,
            "referred_for_usg": visit.referred_for_usg,
            "is_emergency": visit.is_emergency,
            "doctor_notes": visit.doctor_notes,
            "next_visit_date": visit.next_visit_date,
            "facility_name": visit.facility_name,
            "attended_by": visit.attended_by,
            "created_at": visit.created_at,
            "updated_at": visit.updated_at,
            "lmp_date": pw.lmp_date if pw else None,
            "gestational_weeks": tracking["gestational_weeks"],
            "tracking_type": tracking["tracking_type"],
            
        })
    return result

@router.put("/{visit_id}")
async def update_anc_visit(
    visit_id: int,
    visit_update: ANCVisitUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Update ANC visit record (Sub-Centre/Block users)"""
    if current_user.role not in ["sub_centre", "block"]:
        raise HTTPException(status_code=403, detail="Only Sub-Centre or Block users can update ANC visits")
    
    visit = db.query(ANCVisit).filter(ANCVisit.id == visit_id).first()
    if not visit:
        raise HTTPException(status_code=404, detail="ANC visit not found")
    
    # Authorization check
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == visit.pregnant_woman_id).first()
    if current_user.role == "sub_centre":
        if pw.sub_centre_id is not None and pw.sub_centre_id != current_user.sub_centre_id:
            if pw.block_id != current_user.block_id:
                raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "block" and pw.block_id != current_user.block_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Update fields
    old_values = get_entity_snapshot(visit)
    update_data = visit_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(visit, field, value)

    visit.updated_at = datetime.now()
    db.commit()
    db.refresh(visit)

    ip_address, user_agent = get_client_info(request)
    log_update(
        db,
        current_user.id,
        "ANCVisit",
        visit.id,
        old_values,
        get_entity_snapshot(visit),
        ip_address,
        user_agent
    )

    # --- Auto High-Risk Detection ---
    high_risk_triggered = False
    risk_reasons = evaluate_high_risk(visit)

    if risk_reasons and not pw.is_high_risk:
        _apply_high_risk_flag(db, pw, risk_reasons, current_user)
        high_risk_triggered = True
        logger.info(
            f"[HIGH-RISK AUTO] pw_id={pw.id} name='{pw.full_name}' "
            f"visit_id={visit.id} reasons={risk_reasons}"
        )

    return {
        "id": visit.id,
        "pregnant_woman_id": visit.pregnant_woman_id,
        "visit_number": visit.visit_number,
        "visit_date": visit.visit_date,
        "weight": visit.weight,
        "blood_pressure": visit.blood_pressure,
        "hemoglobin": visit.hemoglobin,
        "urine_albumin": visit.urine_albumin,   # NEW
        "urine_sugar": visit.urine_sugar,
        "fundal_height": visit.fundal_height,
        "fetal_heart_rate": visit.fetal_heart_rate,
        "referred_for_usg": visit.referred_for_usg,
        "is_emergency": visit.is_emergency,
        "doctor_notes": visit.doctor_notes,
        "next_visit_date": visit.next_visit_date,
        "facility_name": visit.facility_name,
        "attended_by": visit.attended_by,
        "created_at": visit.created_at,
        "updated_at": visit.updated_at,
        "high_risk_triggered": high_risk_triggered,
        "risk_reasons": risk_reasons,
    }

@router.delete("/{visit_id}")
async def delete_anc_visit(
    visit_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Delete ANC visit record (District/Block users only)"""
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=403, detail="Only District or Block users can delete ANC visits")
    
    visit = db.query(ANCVisit).filter(ANCVisit.id == visit_id).first()
    if not visit:
        raise HTTPException(status_code=404, detail="ANC visit not found")
    
    old_values = get_entity_snapshot(visit)
    db.delete(visit)
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_delete(
        db,
        current_user.id,
        "ANCVisit",
        visit.id,
        old_values,
        ip_address,
        user_agent
    )
    
    return {"message": "ANC visit deleted successfully"}

@router.get("/pregnant-woman/{pw_id}")
async def get_visits_for_pregnant_woman(
    pw_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get all ANC visits for a specific pregnant woman"""
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pw_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")
    
    # Authorization check
    if current_user.role == "district" and pw.district_id != current_user.district_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "block" and pw.block_id != current_user.block_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "sub_centre":
        if pw.sub_centre_id is not None and pw.sub_centre_id != current_user.sub_centre_id:
            if pw.block_id != current_user.block_id:
                raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "pmsma" and pw.block_id != current_user.block_id:
        # PMSMA users (block level) see the woman's details in the PMSMA session popup
        raise HTTPException(status_code=403, detail="Not authorized")
    
    visits = db.query(ANCVisit).filter(
        ANCVisit.pregnant_woman_id == pw_id
    ).order_by(ANCVisit.visit_number).all()
    
    # Calculate next due date
    next_due_date = None
    if visits:
        last_visit = visits[-1]
        if last_visit.next_visit_date:
            next_due_date = last_visit.next_visit_date
    
    from models import Ward, SubCentre
    ward = db.query(Ward).filter(Ward.id == pw.ward_id).first() if pw.ward_id else None
    sub_centre = db.query(SubCentre).filter(SubCentre.id == pw.sub_centre_id).first() if pw.sub_centre_id else None

    return {
        "pregnant_woman": {
            "id": pw.id,
            "full_name": pw.full_name,
            "mobile_number": pw.mobile_number,
            "edd_date": pw.edd_date,
            "is_high_risk": pw.is_high_risk,
            # Basic details (shown in the session / visit detail popups)
            "age": pw.age,
            "husband_name": pw.husband_name,
            "address": pw.address,
            "rch_id": pw.rch_id,
            "lmp_date": pw.lmp_date,
            "gravida": pw.gravida,
            "para": pw.para,
            "blood_group": pw.blood_group,
            "risk_factors": pw.risk_factors,
            "ward_name": ward.name if ward else None,
            "sub_centre_name": sub_centre.name if sub_centre else None,
        },
        "visits": visits,
        "total_visits": len(visits),
        "next_due_date": next_due_date
    }

@router.get("/due/today")
async def get_visits_due_today(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get ANC visits scheduled for today"""
    if current_user.role not in ["district", "sub_centre", "block"]:
        raise HTTPException(status_code=403, detail="Only District, Sub-Centre or Block users can access this")
    
    today = date.today()
    
    # Get last visits with next_visit_date = today
    query = db.query(ANCVisit).filter(ANCVisit.next_visit_date == today)
    
    from models import UserWardMapping, WardSubcentreMapping
    from sqlalchemy import or_
    if current_user.role == "sub_centre":
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
    elif current_user.role == "block":
        query = query.join(PregnantWoman).filter(PregnantWoman.block_id == current_user.block_id)
    elif current_user.role == "district":
        query = query.join(PregnantWoman).filter(PregnantWoman.district_id == current_user.district_id)
    
    visits = query.all()
    
    result = []
    for visit in visits:
        pw = db.query(PregnantWoman).filter(PregnantWoman.id == visit.pregnant_woman_id).first()
        tracking = get_tracking_info(pw)
        result.append({
            "pregnant_woman_id": pw.id,
            "pregnant_woman_name": pw.full_name,
            "mobile_number": pw.mobile_number,
            "last_visit_date": visit.visit_date,
            "last_visit_number": visit.visit_number,
            "due_date": today,
            "gestational_weeks": tracking["gestational_weeks"],
            "tracking_type": tracking["tracking_type"],
        })
    
    return result

@router.post("/notify-due-visits")
async def notify_due_anc_visits(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Send notifications for ANC visits due today (scheduled job)
    """
    if current_user.role not in ["district", "block", "sub_centre"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only district, block, or sub-centre users can run this operation"
        )
    
    today = date.today()
    
    # Get last visits with next_visit_date = today
    query = db.query(ANCVisit).filter(ANCVisit.next_visit_date == today)
    
    from models import UserWardMapping, WardSubcentreMapping
    from sqlalchemy import or_
    if current_user.role == "sub_centre":
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
    elif current_user.role == "block":
        query = query.join(PregnantWoman).filter(PregnantWoman.block_id == current_user.block_id)
    elif current_user.role == "district":
        query = query.join(PregnantWoman).filter(PregnantWoman.district_id == current_user.district_id)
    
    visits = query.all()
    notified_count = 0
    
    for visit in visits:
        pw = db.query(PregnantWoman).filter(PregnantWoman.id == visit.pregnant_woman_id).first()
        
        if pw and pw.sub_centre_id:
            # Notify sub-centre users
            sub_centre_users = db.query(User).filter(
                User.sub_centre_id == pw.sub_centre_id,
                User.role == "sub_centre",
                User.is_active == True
            ).all()
            
            if sub_centre_users:
                NotificationService.create_notification(
                    db=db,
                    user_ids=[u.id for u in sub_centre_users],
                    title="ANC Visit Due Today",
                    message=f"ANC Visit #{visit.visit_number + 1} for {pw.full_name} is due today",
                    notification_type="anc_visit_reminder",
                    category="anc_visit",
                    priority="normal",
                    reference_id=visit.id,
                    reference_type="anc_visit",
                    action_url=f"/pregnant-women/{pw.id}",
                    metadata={
                        "pregnant_woman_name": pw.full_name,
                        "next_visit_number": visit.visit_number + 1,
                        "due_date": today.isoformat()
                    }
                )
                notified_count += 1
    
    return {
        "message": f"Notified about {notified_count} ANC visits due today",
        "count": notified_count
    }

@router.post("/notify-overdue-visits")
async def notify_overdue_anc_visits(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Overdue ANC notification -> only the PW's own sub-centre users."""
    if current_user.role not in ["district", "block", "sub_centre"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    return send_overdue_notifications(db)

@router.get("/overdue/list")
async def get_overdue_visits(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get overdue ANC visits (past next_visit_date)"""
    today = date.today()
    
    # Get last visits with next_visit_date < today
    query = db.query(ANCVisit).filter(
        ANCVisit.next_visit_date < today,
        ANCVisit.next_visit_date.isnot(None)
    )
    
    from models import UserWardMapping, WardSubcentreMapping
    from sqlalchemy import or_
    if current_user.role == "district":
        query = query.join(PregnantWoman).filter(PregnantWoman.district_id == current_user.district_id)
    elif current_user.role == "block":
        query = query.join(PregnantWoman).filter(PregnantWoman.block_id == current_user.block_id)
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
    
    visits = query.all()
    
    result = []
    for visit in visits:
        # Check if there's a newer visit
        newer_visit = db.query(ANCVisit).filter(
            ANCVisit.pregnant_woman_id == visit.pregnant_woman_id,
            ANCVisit.visit_number > visit.visit_number
        ).first()
        
        if not newer_visit:  # Only include if no newer visit exists
            pw = db.query(PregnantWoman).filter(PregnantWoman.id == visit.pregnant_woman_id).first()
            days_overdue = (today - visit.next_visit_date).days
            
            # ==================== BUG FIX SL NO. 49 ====================
            # Updated response structure to match standard API contract
            # Fixed field names: last_visit_number → visit_number, last_visit_date → visit_date, next_visit_due → next_visit_date
            # Added missing critical fields: id, facility_name, referred_for_usg, is_emergency
            # Maintains backward compatibility by keeping days_overdue and sub_centre_name
            tracking = get_tracking_info(pw)
            result.append({
                "id": visit.id,  # ADDED - Required for Edit/Delete operations
                "pregnant_woman_id": pw.id,
                "pregnant_woman_name": pw.full_name,
                "mobile_number": pw.mobile_number,
                "visit_number": visit.visit_number,  # FIXED - was last_visit_number
                "visit_date": visit.visit_date,  # FIXED - was last_visit_date
                "next_visit_date": visit.next_visit_date,  # FIXED - was next_visit_due
                "facility_name": visit.facility_name,  # ADDED - Required for table display
                "referred_for_usg": visit.referred_for_usg,  # ADDED - Required for badge type
                "is_emergency": visit.is_emergency,  # ADDED - Required for badge type
                "weight": visit.weight,  # ADDED - Clinical detail
                "blood_pressure": visit.blood_pressure,  # ADDED - Clinical detail
                "hemoglobin": visit.hemoglobin,  # ADDED - Clinical detail
                "fundal_height": visit.fundal_height,  # ADDED - Clinical detail
                "fetal_heart_rate": visit.fetal_heart_rate,  # ADDED - Clinical detail
                "doctor_notes": visit.doctor_notes,  # ADDED - Clinical notes
                "attended_by": visit.attended_by,  # ADDED - Healthcare provider
                "created_at": visit.created_at.isoformat() if visit.created_at else None,  # ADDED - Timestamp
                "updated_at": visit.updated_at.isoformat() if visit.updated_at else None,  # ADDED - Timestamp
                "days_overdue": days_overdue,  # KEPT - Overdue-specific field
                "sub_centre_name": pw.sub_centre.name if pw.sub_centre else None,  # KEPT - Additional context
                "gestational_weeks": tracking["gestational_weeks"],
                "tracking_type": tracking["tracking_type"],
            })
            # ==================== END BUG FIX ====================
    
    return result

@router.get("/{visit_id}")
async def get_anc_visit(
    visit_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get specific ANC visit details"""
    visit = db.query(ANCVisit).filter(ANCVisit.id == visit_id).first()
    if not visit:
        raise HTTPException(status_code=404, detail="ANC visit not found")
    
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == visit.pregnant_woman_id).first()
    if current_user.role == "district" and pw.district_id != current_user.district_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "block" and pw.block_id != current_user.block_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "sub_centre":
        if pw.sub_centre_id is not None and pw.sub_centre_id != current_user.sub_centre_id:
            if pw.block_id != current_user.block_id:
                raise HTTPException(status_code=403, detail="Not authorized")
    
    return {
        "id": visit.id,
        "pregnant_woman_id": visit.pregnant_woman_id,
        "visit_number": visit.visit_number,
        "visit_date": visit.visit_date,
        "weight": visit.weight,
        "blood_pressure": visit.blood_pressure,
        "hemoglobin": visit.hemoglobin,
        "fundal_height": visit.fundal_height,
        "fetal_heart_rate": visit.fetal_heart_rate,
        "referred_for_usg": visit.referred_for_usg,
        "is_emergency": visit.is_emergency,
        "doctor_notes": visit.doctor_notes,
        "next_visit_date": visit.next_visit_date,
        "facility_name": visit.facility_name,
        "attended_by": visit.attended_by,
        "created_at": visit.created_at,
        "updated_at": visit.updated_at,
        "high_risk_triggered": False,
        "risk_reasons": [],
    }

@router.get("/statistics/summary")
async def get_anc_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get ANC visit statistics"""
    query = db.query(ANCVisit)
    
    # Role-based filtering
    from models import UserWardMapping, WardSubcentreMapping
    from sqlalchemy import or_
    if current_user.role == "district":
        query = query.join(PregnantWoman).filter(PregnantWoman.district_id == current_user.district_id)
    elif current_user.role == "block":
        query = query.join(PregnantWoman).filter(PregnantWoman.block_id == current_user.block_id)
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
    
    total_visits = query.count()
    
    # Visits by number
    visit_1 = query.filter(ANCVisit.visit_number == 1).count()
    visit_2 = query.filter(ANCVisit.visit_number == 2).count()
    visit_3 = query.filter(ANCVisit.visit_number == 3).count()
    visit_4 = query.filter(ANCVisit.visit_number == 4).count()
    
    # USG referrals
    usg_referrals = query.filter(ANCVisit.referred_for_usg == True).count()
    
    # Emergency visits
    emergency_visits = query.filter(ANCVisit.is_emergency == True).count()
    
    # This month's visits
    today = date.today()
    first_day = today.replace(day=1)
    this_month = query.filter(ANCVisit.visit_date >= first_day).count()
    
    return {
        "total_anc_visits": total_visits,
        "visits_by_number": {
            "first_anc": visit_1,
            "second_anc": visit_2,
            "third_anc": visit_3,
            "fourth_anc": visit_4
        },
        "usg_referrals": usg_referrals,
        "emergency_visits": emergency_visits,
        "visits_this_month": this_month
    }