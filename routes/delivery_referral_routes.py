from fastapi import APIRouter, Depends, HTTPException, status, Request, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Optional
from datetime import datetime, timedelta
import os
import uuid
import shutil
import json
import logging

from database import get_db
from models import (
    DeliveryReferral, DeliveryOutcome, DeliveryOutcomeBaby, DeliveryPoint,
    PregnantWoman, ANCVisit, USGAppointment, User, ReferralStatus, SubCentre,
    Discharge, PNCReminder, PNCReminderStatus, Admission, MaternalOutcome
)
from schemas import (
    DeliveryReferralCreate, DeliveryReferralReRefer,
    DeliveryReferralResponse, DeliveryOutcomeCreate, DeliveryOutcomeResponse,
    DischargeCreate, AdmissionCreate, AdmissionUpdate
)
from auth import get_current_active_user
from audit_utils import get_client_info, get_entity_snapshot, log_create, log_update
from services.notification_service import NotificationService
from file_validator import validate_upload_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/delivery-referrals", tags=["Delivery Referrals"])

OTHER_DP_CODE = "OTHER_DP_GLOBAL"

# PNC reminder windows — standard post-natal visit schedule (days after delivery).
# Configurable via env var (comma-separated "label:days" pairs) rather than hardcoded,
# e.g. PNC_REMINDER_OFFSETS="48hr:2,day7:7,day42:42"
def _load_pnc_reminder_offsets() -> list:
    raw = os.getenv("PNC_REMINDER_OFFSETS", "48hr:2,day7:7,day42:42")
    offsets = []
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        label, days = part.split(":", 1)
        try:
            offsets.append((label.strip(), int(days.strip())))
        except ValueError:
            continue
    return offsets or [("48hr", 2), ("day7", 7), ("day42", 42)]


PNC_REMINDER_OFFSETS = _load_pnc_reminder_offsets()


def get_other_dp_id(db: Session) -> int:
    """Return the id of the global Other DP Point, or None if not seeded."""
    dp = db.query(DeliveryPoint).filter(DeliveryPoint.code == OTHER_DP_CODE).first()
    return dp.id if dp else None


async def save_referral_attachments(files: List[UploadFile]) -> List[str]:
    """Validate and save referral attachment files, return list of paths"""
    upload_dir = "uploads/referral_attachments"
    os.makedirs(upload_dir, exist_ok=True)
    paths = []
    for f in files:
        if not f or not f.filename:
            continue
        await validate_upload_file(f, "attachment")
        ext = os.path.splitext(f.filename)[1]
        file_path = os.path.join(upload_dir, f"{uuid.uuid4()}{ext}")
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(f.file, buffer)
        paths.append(file_path)
    return paths


def _resolve_babies(outcome: DeliveryOutcome) -> list:
    """Return babies list with backward-compat fallback to legacy baby_gender."""
    if outcome.babies:
        return [
            {"baby_number": b.baby_number, "gender": b.gender, "status": b.status}
            for b in sorted(outcome.babies, key=lambda x: x.baby_number)
        ]
    if outcome.baby_gender:
        return [{"baby_number": 1, "gender": outcome.baby_gender, "status": "live_birth"}]
    return []


def build_referral_chain(db: Session, referral: DeliveryReferral) -> list:
    """Walk back through previous_referral_id to build full chain (oldest first)"""
    chain = []
    current = referral
    while current:
        dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == current.dp_id).first()
        referred_by = db.query(User).filter(User.id == current.referred_by_user_id).first()
        chain.append({
            "referral_id": current.id,
            "dp_id": current.dp_id,
            "dp_name": dp.name if dp else None,
            "status": current.status,
            "observation_notes": current.observation_notes,
            "re_refer_reason": current.re_refer_reason,
            "treatment_given": current.treatment_given,
            "re_refer_attachment_urls": [
                f"/uploads/referral_attachments/{os.path.basename(p)}"
                for p in json.loads(current.re_refer_attachment_paths)
            ] if current.re_refer_attachment_paths else [],
            "referred_by": referred_by.full_name if referred_by else None,
            "referred_by_role": referred_by.role if referred_by else None,
            "created_at": current.created_at,
        })
        if current.previous_referral_id:
            current = db.query(DeliveryReferral).filter(
                DeliveryReferral.id == current.previous_referral_id
            ).first()
        else:
            current = None
    chain.reverse()  # oldest first
    return chain


def format_referral_detail(db: Session, referral: DeliveryReferral) -> dict:
    """Full referral detail with PW profile, ANC visits, USG history, and chain"""
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == referral.pregnant_woman_id).first()
    dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == referral.dp_id).first()

    anc_visits = db.query(ANCVisit).filter(
        ANCVisit.pregnant_woman_id == referral.pregnant_woman_id
    ).order_by(ANCVisit.visit_number).all()

    usg_appointments = db.query(USGAppointment).filter(
        USGAppointment.pregnant_woman_id == referral.pregnant_woman_id
    ).order_by(USGAppointment.created_at.desc()).all()

    outcome = db.query(DeliveryOutcome).filter(
        DeliveryOutcome.referral_id == referral.id
    ).first()

    discharge = db.query(Discharge).filter(Discharge.referral_id == referral.id).first()
    discharged_by_user = db.query(User).filter(User.id == discharge.discharged_by).first() if discharge else None

    admission = db.query(Admission).filter(Admission.referral_id == referral.id).first()
    admitted_by_user = db.query(User).filter(User.id == admission.admitted_by).first() if admission else None

    return {
        "referral": {
            "id": referral.id,
            "status": referral.status,
            "observation_notes": referral.observation_notes,
            "re_refer_reason": referral.re_refer_reason,
            "treatment_given": referral.treatment_given,
            "re_refer_attachment_urls": [
                f"/uploads/referral_attachments/{os.path.basename(p)}"
                for p in json.loads(referral.re_refer_attachment_paths)
            ] if referral.re_refer_attachment_paths else [],
            "accepted_at": referral.accepted_at,
            "created_at": referral.created_at,
            "updated_at": referral.updated_at,
            "dp_id": referral.dp_id,
            "dp_name": dp.name if dp else None,
            "previous_referral_id": referral.previous_referral_id,
        },
        "pregnant_woman": {
            "id": pw.id,
            "full_name": pw.full_name,
            "mobile_number": pw.mobile_number,
            "age": pw.age,
            "husband_name": pw.husband_name,
            "address": pw.address,
            "blood_group": pw.blood_group,
            "lmp_date": pw.lmp_date,
            "edd_date": pw.edd_date,
            "gravida": pw.gravida,
            "para": pw.para,
            "is_high_risk": pw.is_high_risk,
            "risk_factors": pw.risk_factors,
            "abha_id": pw.abha_id,
            "rch_id": pw.rch_id,
        },
        "anc_visits": [
            {
                "visit_number": v.visit_number,
                "visit_date": v.visit_date,
                "weight": v.weight,
                "blood_pressure": v.blood_pressure,
                "hemoglobin": v.hemoglobin,
                "fundal_height": v.fundal_height,
                "fetal_heart_rate": v.fetal_heart_rate,
                "doctor_notes": v.doctor_notes,
                "referred_for_usg": v.referred_for_usg,
                "is_emergency": v.is_emergency,
            }
            for v in anc_visits
        ],
        "usg_appointments": [
            {
                "id": a.id,
                "usg_centre_id": a.usg_centre_id,
                "scheduled_date": a.scheduled_date,
                "appointment_type": a.appointment_type,
                "status": a.status,
                "scan_date": a.scan_date,
                "completed_date": a.completed_date,
                "trimester": a.trimester,
                "scan_type": a.scan_type,
                "gestational_age": a.gestational_age,
                "findings": a.findings,
                "abnormal_findings": a.abnormal_findings,
                "additional_notes": a.additional_notes,
                "usg_findings": a.usg_findings,
                "doctor_name": a.doctor_name,
                "technician_name": a.technician_name,
                "is_high_risk": a.is_high_risk,
                "reschedule_count": a.reschedule_count,
                "reschedule_reason": a.reschedule_reason,
                "prescription_file_url": f"/uploads/prescriptions/{os.path.basename(a.prescription_file_path)}" if a.prescription_file_path else None,
                "report_file_url": f"/uploads/usg_reports/{os.path.basename(a.report_file_path)}" if a.report_file_path else None,
                "prescription_file_urls": [
                    f"/uploads/prescriptions/{os.path.basename(p)}"
                    for p in json.loads(a.prescription_file_paths)
                ] if a.prescription_file_paths else [],
                "report_file_urls": [
                    f"/uploads/usg_reports/{os.path.basename(p)}"
                    for p in json.loads(a.report_file_paths)
                ] if a.report_file_paths else [],
                "created_at": a.created_at,
            }
            for a in usg_appointments
        ],
        "referral_chain": build_referral_chain(db, referral),
        "admission": {
            "id": admission.id,
            "admission_date": admission.admission_date,
            "treating_doctor": admission.treating_doctor,
            "condition_at_admission": admission.condition_at_admission,
            "discharge_date": admission.discharge_date,
            "days_admitted": admission.days_admitted,
            "admitted_by_name": admitted_by_user.full_name if admitted_by_user else None,
            "created_at": admission.created_at,
        } if admission else None,
        "outcome": {
            "delivery_type": outcome.delivery_type,
            "delivery_date": outcome.delivery_date,
            "baby_gender": outcome.baby_gender,
            "baby_count": outcome.baby_count,
            "babies": _resolve_babies(outcome),
            "maternal_outcome": outcome.maternal_outcome,
            "maternal_outcome_notes": outcome.maternal_outcome_notes,
            "remarks": outcome.remarks,
            "created_at": outcome.created_at,
        } if outcome else None,
        "discharge": {
            "id": discharge.id,
            "discharge_date": discharge.discharge_date,
            "discharge_facility": discharge.discharge_facility,
            "discharging_doctor": discharge.discharging_doctor,
            "condition_at_discharge": discharge.condition_at_discharge,
            "discharge_notes": discharge.discharge_notes,
            "discharged_by_name": discharged_by_user.full_name if discharged_by_user else None,
            "created_at": discharge.created_at,
        } if discharge else None,
    }


# ─── Available Delivery Points (risk-filtered) ──────────────────────────────

@router.get("/delivery-points/available", response_model=list)
async def get_available_delivery_points(
    pregnant_woman_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Return delivery points filtered by the pregnant woman's risk status.
    High-risk → SDH/DHH only (is_sdh_dhh=True). Normal → full active list.
    Accessible by sub_centre, dp, and pmsma roles."""
    if current_user.role not in ["sub_centre", "dp", "pmsma", "block", "district"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pregnant_woman_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")

    query = db.query(DeliveryPoint).filter(DeliveryPoint.is_active == True)
    if pw.is_high_risk:
        query = query.filter(DeliveryPoint.is_sdh_dhh == True)

    dps = query.order_by(DeliveryPoint.name).all()
    return [
        {
            "id": dp.id,
            "name": dp.name,
            "code": dp.code,
            "address": dp.address,
            "contact_number": dp.contact_number,
            "district_id": dp.district_id,
            "block_id": dp.block_id,
            "is_sdh_dhh": dp.is_sdh_dhh,
            "is_active": dp.is_active,
        }
        for dp in dps
    ]


# ─── Sub-Centre: Create Referral ────────────────────────────────────────────

@router.post("/", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_referral(
    referral_data: DeliveryReferralCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Sub-Centre or DP creates a delivery referral for a pregnant woman.
    DP use case: pregnant woman walks in directly without sub-centre referral."""
    if current_user.role not in ["sub_centre", "dp", "pmsma"]:
        raise HTTPException(status_code=403, detail="Only Sub-Centre, Delivery Point, or PMSMA users can create delivery referrals")

    pw = db.query(PregnantWoman).filter(PregnantWoman.id == referral_data.pregnant_woman_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")

    # Sub-centre: must own the pregnant woman (ward mapping OR sub_centre_id)
    # DP: no sub-centre ownership check — woman walked in directly
    # PMSMA: no sub-centre ownership check — treated like DP walk-in
    if current_user.role == "sub_centre":
        from models import UserWardMapping, WardSubcentreMapping
        authorized = False
        if pw.sub_centre_id == current_user.sub_centre_id:
            authorized = True
        elif pw.sub_centre_id is None:
            authorized = True
        else:
            # Check ward mapping
            user_wards = db.query(UserWardMapping.ward_id).filter(UserWardMapping.user_id == current_user.id).all()
            if user_wards:
                ward_ids = [w.ward_id for w in user_wards]
                authorized = pw.ward_id in ward_ids
            else:
                mapped_ward_ids = [w.ward_id for w in db.query(WardSubcentreMapping.ward_id).filter(
                    WardSubcentreMapping.sub_centre_id == current_user.sub_centre_id
                ).all()]
                authorized = pw.ward_id in mapped_ward_ids or pw.block_id == current_user.block_id
        if not authorized:
            raise HTTPException(status_code=403, detail="Not authorized for this pregnant woman")

    # Check no active referral already exists
    active = db.query(DeliveryReferral).filter(
        DeliveryReferral.pregnant_woman_id == referral_data.pregnant_woman_id,
        DeliveryReferral.status.in_(["pending", "accepted"])
    ).first()
    if active:
        raise HTTPException(status_code=409, detail="An active referral already exists for this pregnant woman")

    # DP user chooses target dp_id from request; sub_centre user also picks dp_id
    if current_user.role == "dp":
        dp_id = referral_data.dp_id
        sub_centre_id = None
    elif current_user.role == "pmsma":
        dp_id = referral_data.dp_id
        sub_centre_id = None
    else:
        dp_id = referral_data.dp_id
        sub_centre_id = current_user.sub_centre_id

    dp = db.query(DeliveryPoint).filter(
        DeliveryPoint.id == dp_id,
        DeliveryPoint.is_active == True
    ).first()
    if not dp:
        raise HTTPException(status_code=404, detail="Delivery Point not found or inactive")

    other_dp_id = get_other_dp_id(db)
    is_other_dp = (dp_id == other_dp_id)

    referral = DeliveryReferral(
        pregnant_woman_id=referral_data.pregnant_woman_id,
        referred_by_user_id=current_user.id,
        sub_centre_id=sub_centre_id,
        dp_id=dp_id,
        observation_notes=referral_data.observation_notes,
        status=ReferralStatus.ACCEPTED if is_other_dp else ReferralStatus.PENDING,
        accepted_by_user_id=current_user.id if is_other_dp else None,
        accepted_at=datetime.now() if is_other_dp else None,
    )
    db.add(referral)

    # Update PW status
    pw.pregnancy_outcome = "referred"
    db.commit()
    db.refresh(referral)

    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "DeliveryReferral", referral.id, get_entity_snapshot(referral), ip_address, user_agent)

    # Other DP Point — notify the sub-centre creator directly (auto-accepted)
    if is_other_dp and current_user.role == "sub_centre":
        NotificationService.create_notification(
            db=db,
            user_ids=[current_user.id],
            title="Referral Auto-Accepted",
            message=f"Referral for {pw.full_name} to Other DP Point is auto-accepted. You can now record the delivery outcome.",
            notification_type="delivery_referral_accepted",
            category="delivery",
            priority="normal",
            reference_id=referral.id,
            reference_type="delivery_referral",
            action_url="/delivery-referral-management",
            metadata={"pregnant_woman_name": pw.full_name}
        )

    # Notify DP users (when sub_centre or pmsma creates and NOT Other DP)
    if current_user.role in ("sub_centre", "pmsma") and not is_other_dp:
        dp_users = db.query(User).filter(
            User.dp_id == dp_id,
            User.is_active == True
        ).all()
        if dp_users:
            NotificationService.create_notification(
                db=db,
                user_ids=[u.id for u in dp_users],
                title="New Delivery Referral",
                message=f"New delivery referral received for {pw.full_name} from Sub-Centre",
                notification_type="delivery_referral_new",
                category="delivery",
                priority="high",
                reference_id=referral.id,
                reference_type="delivery_referral",
                action_url=f"/delivery-referrals/{referral.id}",
                metadata={"pregnant_woman_name": pw.full_name}
            )

    # Notify target DP users when a DP user refers to another DP (not Other DP)
    if current_user.role == "dp" and not is_other_dp:
        target_dp_users = db.query(User).filter(
            User.dp_id == dp_id,
            User.is_active == True
        ).all()
        if target_dp_users:
            NotificationService.create_notification(
                db=db,
                user_ids=[u.id for u in target_dp_users],
                title="New Delivery Referral",
                message=f"New delivery referral received for {pw.full_name} from {dp.name if dp else 'another Delivery Point'}",
                notification_type="delivery_referral_new",
                category="delivery",
                priority="high",
                reference_id=referral.id,
                reference_type="delivery_referral",
                action_url=f"/delivery-referrals/{referral.id}",
                metadata={"pregnant_woman_name": pw.full_name}
            )

    return {"message": "Referral created successfully", "referral_id": referral.id, "is_other_dp": is_other_dp}


# ─── List Referrals (role-filtered) ─────────────────────────────────────────

@router.get("/", response_model=List[dict])
async def get_referrals(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get delivery referrals filtered by role"""
    query = db.query(DeliveryReferral)

    if current_user.role == "sub_centre":
        from models import UserWardMapping, WardSubcentreMapping
        user_wards = db.query(UserWardMapping.ward_id).filter(UserWardMapping.user_id == current_user.id).all()
        if user_wards:
            ward_ids = [w.ward_id for w in user_wards]
            pw_ids = db.query(PregnantWoman.id).filter(PregnantWoman.ward_id.in_(ward_ids)).subquery()
        else:
            mapped_ward_ids = db.query(WardSubcentreMapping.ward_id).filter(
                WardSubcentreMapping.sub_centre_id == current_user.sub_centre_id
            ).subquery()
            pw_ids = db.query(PregnantWoman.id).filter(
                or_(PregnantWoman.ward_id.in_(mapped_ward_ids), PregnantWoman.sub_centre_id == current_user.sub_centre_id)
            ).subquery()
        query = query.filter(
            or_(
                DeliveryReferral.sub_centre_id == current_user.sub_centre_id,
                DeliveryReferral.pregnant_woman_id.in_(pw_ids)
            )
        )
    elif current_user.role == "dp":
        query = query.filter(DeliveryReferral.dp_id == current_user.dp_id)
    elif current_user.role == "pmsma":
        pw_ids = db.query(PregnantWoman.id).filter(PregnantWoman.block_id == current_user.block_id).subquery()
        query = query.filter(
            or_(
                DeliveryReferral.referred_by_user_id == current_user.id,
                DeliveryReferral.pregnant_woman_id.in_(pw_ids)
            )
        )
    elif current_user.role == "block":
        pw_ids = db.query(PregnantWoman.id).filter(PregnantWoman.block_id == current_user.block_id).subquery()
        query = query.filter(DeliveryReferral.pregnant_woman_id.in_(pw_ids))
    elif current_user.role == "district":
        pw_ids = db.query(PregnantWoman.id).filter(PregnantWoman.district_id == current_user.district_id).subquery()
        query = query.filter(DeliveryReferral.pregnant_woman_id.in_(pw_ids))

    if status:
        query = query.filter(DeliveryReferral.status == status)

    referrals = query.order_by(DeliveryReferral.created_at.desc()).all()

    result = []
    for r in referrals:
        pw = db.query(PregnantWoman).filter(PregnantWoman.id == r.pregnant_woman_id).first()
        dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == r.dp_id).first()
        sc = db.query(SubCentre).filter(SubCentre.id == r.sub_centre_id).first() if r.sub_centre_id else None

        # Build referral chain summary string: "SC Pipili → PHC Sadar → District Hospital"
        chain = build_referral_chain(db, r)
        if sc and chain:
            chain_names = [sc.name] + [c["dp_name"] for c in chain if c["dp_name"]]
            # Deduplicate consecutive duplicates
            chain_summary = " → ".join(dict.fromkeys(chain_names))
        elif chain:
            chain_summary = " → ".join(dict.fromkeys([c["dp_name"] for c in chain if c["dp_name"]]))
        else:
            chain_summary = dp.name if dp else None

        discharge = db.query(Discharge).filter(Discharge.referral_id == r.id).first()
        admission = db.query(Admission).filter(Admission.referral_id == r.id).first()
        outcome = db.query(DeliveryOutcome).filter(DeliveryOutcome.referral_id == r.id).first()
        _dt = getattr(outcome.delivery_type, "value", outcome.delivery_type) if outcome else None
        _mo = getattr(outcome.maternal_outcome, "value", outcome.maternal_outcome) if outcome else None
        infant_death_count = (
            sum(1 for b in outcome.babies if b.status == "infant_death") if outcome else 0
        )

        result.append({
            "delivery_type": _dt,
            "maternal_outcome": _mo,
            "is_maternal_death": _mo == "maternal_death" or _dt == "maternal_death",
            "infant_death_count": infant_death_count,
            "id": r.id,
            "pregnant_woman_id": r.pregnant_woman_id,
            "pregnant_woman_name": pw.full_name if pw else None,
            "mobile_number": pw.mobile_number if pw else None,
            "is_high_risk": bool(pw.is_high_risk) if pw else False,
            "risk_factors": pw.risk_factors if pw else None,
            "dp_id": r.dp_id,
            "dp_name": dp.name if dp else None,
            "sub_centre_id": r.sub_centre_id,
            "sub_centre_name": sc.name if sc else None,
            "referral_chain_summary": chain_summary,
            "referral_chain": chain,
            "status": r.status,
            "observation_notes": r.observation_notes,
            "previous_referral_id": r.previous_referral_id,
            "is_discharged": discharge is not None,
            "is_admitted": admission is not None,
            "days_admitted": admission.days_admitted if admission else None,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        })
    return result


# ─── Get Referral Detail with Chain ─────────────────────────────────────────

@router.get("/{referral_id}", response_model=dict)
async def get_referral_detail(
    referral_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get full referral detail: PW profile, ANC visits, USG history, referral chain"""
    referral = db.query(DeliveryReferral).filter(DeliveryReferral.id == referral_id).first()
    if not referral:
        raise HTTPException(status_code=404, detail="Referral not found")

    # Authorization
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == referral.pregnant_woman_id).first()
    if current_user.role == "sub_centre":
        from models import UserWardMapping, WardSubcentreMapping
        # Allow if referral was created by this sub_centre OR woman is in their ward/sub_centre
        sc_match = referral.sub_centre_id == current_user.sub_centre_id
        if not sc_match:
            user_wards = db.query(UserWardMapping.ward_id).filter(UserWardMapping.user_id == current_user.id).all()
            if user_wards:
                ward_ids = [w.ward_id for w in user_wards]
                sc_match = pw.ward_id in ward_ids
            else:
                mapped_ward_ids = [w.ward_id for w in db.query(WardSubcentreMapping.ward_id).filter(
                    WardSubcentreMapping.sub_centre_id == current_user.sub_centre_id
                ).all()]
                sc_match = pw.ward_id in mapped_ward_ids or pw.sub_centre_id == current_user.sub_centre_id
        if not sc_match:
            raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "dp" and referral.dp_id != current_user.dp_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "block" and pw.block_id != current_user.block_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "district" and pw.district_id != current_user.district_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "pmsma" and referral.referred_by_user_id != current_user.id and pw.block_id != current_user.block_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    return format_referral_detail(db, referral)


# ─── DP: Accept Referral (Proceed to Delivery) ──────────────────────────────

@router.post("/{referral_id}/accept", response_model=dict)
async def accept_referral(
    referral_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """DP accepts referral — proceeds to delivery"""
    if current_user.role != "dp":
        raise HTTPException(status_code=403, detail="Only Delivery Point users can accept referrals")

    referral = db.query(DeliveryReferral).filter(
        DeliveryReferral.id == referral_id,
        DeliveryReferral.dp_id == current_user.dp_id
    ).first()
    if not referral:
        raise HTTPException(status_code=404, detail="Referral not found")

    if referral.status != "pending":
        raise HTTPException(status_code=400, detail=f"Referral is already {referral.status}")

    old_values = get_entity_snapshot(referral)
    referral.status = "accepted"
    referral.accepted_by_user_id = current_user.id
    referral.accepted_at = datetime.now()
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_update(db, current_user.id, "DeliveryReferral", referral.id, old_values, get_entity_snapshot(referral), ip_address, user_agent)

    # Notify sub-centre
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == referral.pregnant_woman_id).first()
    if referral.sub_centre_id:
        sc_users = db.query(User).filter(
            User.sub_centre_id == referral.sub_centre_id,
            User.is_active == True
        ).all()
        if sc_users:
            dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == referral.dp_id).first()
            NotificationService.create_notification(
                db=db,
                user_ids=[u.id for u in sc_users],
                title="Referral Accepted",
                message=f"Delivery referral for {pw.full_name} accepted by {dp.name if dp else 'DP'}",
                notification_type="delivery_referral_accepted",
                category="delivery",
                priority="normal",
                reference_id=referral.id,
                reference_type="delivery_referral",
                action_url=f"/delivery-referrals/{referral.id}",
                metadata={"pregnant_woman_name": pw.full_name}
            )

    return {"message": "Referral accepted successfully", "referral_id": referral.id}


# ─── DP: Admission (hospital stay between accepted referral and outcome) ───
# Facility is implicitly the DP itself (dp_id) — a DP *is* the facility here.
# days_admitted is never entered/stored; it's computed on read from
# admission_date/discharge_date (see Admission.days_admitted).

@router.post("/{referral_id}/admission", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_admission(
    referral_id: int,
    admission_data: AdmissionCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """DP records that a referred woman has arrived and been admitted."""
    if current_user.role != "dp":
        raise HTTPException(status_code=403, detail="Only Delivery Point users can record admissions")

    referral = db.query(DeliveryReferral).filter(
        DeliveryReferral.id == referral_id,
        DeliveryReferral.dp_id == current_user.dp_id
    ).first()
    if not referral:
        raise HTTPException(status_code=404, detail="Referral not found")

    if referral.status != "accepted":
        raise HTTPException(status_code=400, detail="Referral must be accepted before recording an admission")

    existing_admission = db.query(Admission).filter(Admission.referral_id == referral_id).first()
    if existing_admission:
        raise HTTPException(status_code=409, detail="An admission has already been recorded for this referral")

    admission = Admission(
        referral_id=referral_id,
        pregnant_woman_id=referral.pregnant_woman_id,
        dp_id=current_user.dp_id,
        admission_date=admission_data.admission_date,
        treating_doctor=admission_data.treating_doctor,
        condition_at_admission=admission_data.condition_at_admission,
        admitted_by=current_user.id,
    )
    db.add(admission)
    db.commit()
    db.refresh(admission)

    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "Admission", admission.id, get_entity_snapshot(admission), ip_address, user_agent)

    # Notify sub-centre
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == referral.pregnant_woman_id).first()
    if referral.sub_centre_id:
        sc_users = db.query(User).filter(
            User.sub_centre_id == referral.sub_centre_id,
            User.is_active == True
        ).all()
        if sc_users:
            dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == referral.dp_id).first()
            NotificationService.create_notification(
                db=db,
                user_ids=[u.id for u in sc_users],
                title="Patient Admitted",
                message=f"{pw.full_name} has been admitted at {dp.name if dp else 'the DP'}",
                notification_type="delivery_admission_recorded",
                category="delivery",
                priority="normal",
                reference_id=admission.id,
                reference_type="admission",
                action_url=f"/delivery-referrals/{referral.id}",
                metadata={"pregnant_woman_name": pw.full_name}
            )

    return {
        "message": "Admission recorded successfully",
        "admission_id": admission.id,
        "referral_id": referral.id,
        "admission_date": admission.admission_date,
        "treating_doctor": admission.treating_doctor,
        "condition_at_admission": admission.condition_at_admission,
        "days_admitted": admission.days_admitted,
    }


@router.put("/{referral_id}/admission", response_model=dict)
async def update_admission(
    referral_id: int,
    admission_data: AdmissionUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """DP updates treating doctor / condition while the woman is admitted,
    or sets discharge_date to close out the stay (independent of the
    separate Discharge/PNC feature, which closes the whole case)."""
    if current_user.role != "dp":
        raise HTTPException(status_code=403, detail="Only Delivery Point users can update admissions")

    referral = db.query(DeliveryReferral).filter(
        DeliveryReferral.id == referral_id,
        DeliveryReferral.dp_id == current_user.dp_id
    ).first()
    if not referral:
        raise HTTPException(status_code=404, detail="Referral not found")

    admission = db.query(Admission).filter(Admission.referral_id == referral_id).first()
    if not admission:
        raise HTTPException(status_code=404, detail="No admission recorded for this referral")

    old_values = get_entity_snapshot(admission)
    if admission_data.treating_doctor is not None:
        admission.treating_doctor = admission_data.treating_doctor
    if admission_data.condition_at_admission is not None:
        admission.condition_at_admission = admission_data.condition_at_admission
    if admission_data.discharge_date is not None:
        admission.discharge_date = admission_data.discharge_date
    db.commit()
    db.refresh(admission)

    ip_address, user_agent = get_client_info(request)
    log_update(db, current_user.id, "Admission", admission.id, old_values, get_entity_snapshot(admission), ip_address, user_agent)

    return {
        "message": "Admission updated successfully",
        "admission_id": admission.id,
        "referral_id": referral.id,
        "treating_doctor": admission.treating_doctor,
        "condition_at_admission": admission.condition_at_admission,
        "discharge_date": admission.discharge_date,
        "days_admitted": admission.days_admitted,
    }


# ─── DP: Re-Refer to Another DP ─────────────────────────────────────────────

@router.post("/{referral_id}/re-refer", response_model=dict)
async def re_refer(
    referral_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    new_dp_id: int = Form(...),
    re_refer_reason: str = Form(...),
    treatment_given: Optional[str] = Form(None),
    attachment_files: Optional[UploadFile] = File(default=None)
):
    """DP re-refers to another DP with reason, optional treatment given and attachments"""
    # DEBUG: log all incoming form data
    logger.info(f"[RE-REFER DEBUG] referral_id={referral_id}")
    logger.info(f"[RE-REFER DEBUG] new_dp_id={new_dp_id}")
    logger.info(f"[RE-REFER DEBUG] re_refer_reason={re_refer_reason}")
    logger.info(f"[RE-REFER DEBUG] treatment_given={treatment_given}")
    logger.info(f"[RE-REFER DEBUG] attachment_files={attachment_files}")
    try:
        form = await request.form()
        logger.info(f"[RE-REFER DEBUG] raw form keys={list(form.keys())}")
    except Exception as e:
        logger.info(f"[RE-REFER DEBUG] could not read raw form: {e}")
    if current_user.role != "dp":
        raise HTTPException(status_code=403, detail="Only Delivery Point users can re-refer")

    referral = db.query(DeliveryReferral).filter(
        DeliveryReferral.id == referral_id,
        DeliveryReferral.dp_id == current_user.dp_id
    ).first()
    if not referral:
        raise HTTPException(status_code=404, detail="Referral not found")

    if referral.status not in ["pending", "accepted"]:
        raise HTTPException(status_code=400, detail="Cannot re-refer a completed or already re-referred referral")

    new_dp = db.query(DeliveryPoint).filter(
        DeliveryPoint.id == new_dp_id,
        DeliveryPoint.is_active == True
    ).first()
    if not new_dp:
        raise HTTPException(status_code=404, detail="Target Delivery Point not found or inactive")

    if new_dp_id == current_user.dp_id:
        raise HTTPException(status_code=400, detail="Cannot re-refer to the same Delivery Point")

    # Save attachment files if provided — read all files from raw form
    attachment_paths = []
    try:
        form = await request.form()
        raw_files = form.getlist("attachment_files")
        valid_files = [f for f in raw_files if hasattr(f, 'filename') and f.filename]
        if valid_files:
            attachment_paths = await save_referral_attachments(valid_files)
    except Exception as e:
        logger.warning(f"[RE-REFER] Could not read attachment_files from form: {e}")

    # Mark current referral as re-referred
    old_values = get_entity_snapshot(referral)
    referral.status = "re_referred"
    referral.re_refer_reason = re_refer_reason
    referral.treatment_given = treatment_given
    referral.re_refer_attachment_path = attachment_paths[0] if attachment_paths else None
    referral.re_refer_attachment_paths = json.dumps(attachment_paths) if attachment_paths else None
    db.commit()

    # Create new referral in chain
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == referral.pregnant_woman_id).first()
    new_referral = DeliveryReferral(
        pregnant_woman_id=referral.pregnant_woman_id,
        referred_by_user_id=current_user.id,
        sub_centre_id=referral.sub_centre_id,
        dp_id=new_dp_id,
        previous_referral_id=referral.id,
        observation_notes=referral.observation_notes,
        status="pending",
    )
    db.add(new_referral)
    db.commit()
    db.refresh(new_referral)

    ip_address, user_agent = get_client_info(request)
    log_update(db, current_user.id, "DeliveryReferral", referral.id, old_values, get_entity_snapshot(referral), ip_address, user_agent)
    log_create(db, current_user.id, "DeliveryReferral", new_referral.id, get_entity_snapshot(new_referral), ip_address, user_agent)

    # Notify new DP users
    new_dp_users = db.query(User).filter(
        User.dp_id == new_dp_id,
        User.is_active == True
    ).all()
    if new_dp_users:
        NotificationService.create_notification(
            db=db,
            user_ids=[u.id for u in new_dp_users],
            title="Delivery Referral Received",
            message=f"Referral for {pw.full_name} forwarded to your Delivery Point",
            notification_type="delivery_referral_new",
            category="delivery",
            priority="high",
            reference_id=new_referral.id,
            reference_type="delivery_referral",
            action_url=f"/delivery-referrals/{new_referral.id}",
            metadata={"pregnant_woman_name": pw.full_name, "re_referred": True}
        )

    # Notify sub-centre
    if referral.sub_centre_id:
        sc_users = db.query(User).filter(
            User.sub_centre_id == referral.sub_centre_id,
            User.is_active == True
        ).all()
        if sc_users:
            NotificationService.create_notification(
                db=db,
                user_ids=[u.id for u in sc_users],
                title="Referral Forwarded",
                message=f"Referral for {pw.full_name} has been forwarded to {new_dp.name}",
                notification_type="delivery_referral_re_referred",
                category="delivery",
                priority="normal",
                reference_id=new_referral.id,
                reference_type="delivery_referral",
                action_url=f"/delivery-referrals/{new_referral.id}",
                metadata={"pregnant_woman_name": pw.full_name, "new_dp_name": new_dp.name}
            )

    return {
        "message": "Re-referred successfully",
        "original_referral_id": referral.id,
        "new_referral_id": new_referral.id,
        "new_dp_name": new_dp.name,
        "treatment_given": treatment_given,
        "attachment_urls": [
            f"/uploads/referral_attachments/{os.path.basename(p)}"
            for p in attachment_paths
        ]
    }


# ─── DP: Record Delivery Outcome ────────────────────────────────────────────

@router.post("/{referral_id}/outcome", response_model=dict)
async def record_outcome(
    referral_id: int,
    outcome_data: DeliveryOutcomeCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """DP records delivery outcome — completes the case"""
    if current_user.role != "dp":
        raise HTTPException(status_code=403, detail="Only Delivery Point users can record delivery outcomes")

    referral = db.query(DeliveryReferral).filter(
        DeliveryReferral.id == referral_id,
        DeliveryReferral.dp_id == current_user.dp_id
    ).first()
    if not referral:
        raise HTTPException(status_code=404, detail="Referral not found")

    if referral.status != "accepted":
        raise HTTPException(status_code=400, detail="Referral must be accepted before recording outcome")

    existing_outcome = db.query(DeliveryOutcome).filter(
        DeliveryOutcome.referral_id == referral_id
    ).first()
    if existing_outcome:
        raise HTTPException(status_code=409, detail="Outcome already recorded for this referral")

    outcome = DeliveryOutcome(
        referral_id=referral_id,
        pregnant_woman_id=referral.pregnant_woman_id,
        dp_id=current_user.dp_id,
        delivery_type=outcome_data.delivery_type,
        delivery_date=outcome_data.delivery_date,
        baby_count=len(outcome_data.babies) if outcome_data.babies else 0,
        # 'maternal_death' delivery type always implies a maternal death outcome
        maternal_outcome=(
            MaternalOutcome.MATERNAL_DEATH
            if getattr(outcome_data.delivery_type, "value", outcome_data.delivery_type) == "maternal_death"
            else outcome_data.maternal_outcome
        ),
        maternal_outcome_notes=outcome_data.maternal_outcome_notes,
        remarks=outcome_data.remarks,
        recorded_by=current_user.id,
    )
    db.add(outcome)

    # Mark referral completed
    old_values = get_entity_snapshot(referral)
    referral.status = "completed"

    # Mark PW case completed
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == referral.pregnant_woman_id).first()
    pw.pregnancy_outcome = outcome_data.delivery_type.value
    pw.outcome_date = outcome_data.delivery_date.date()
    pw.is_active = False

    db.flush()  # get outcome.id before creating babies

    babies_response = []
    if outcome_data.babies:
        for idx, baby in enumerate(outcome_data.babies):
            baby_row = DeliveryOutcomeBaby(
                outcome_id=outcome.id,
                baby_number=idx + 1,
                gender=baby.gender.value,
                status=baby.status,
            )
            db.add(baby_row)
            babies_response.append({"baby_number": idx + 1, "gender": baby.gender.value, "status": baby.status})

    db.commit()
    db.refresh(outcome)

    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "DeliveryOutcome", outcome.id, get_entity_snapshot(outcome), ip_address, user_agent)
    for baby_row in outcome.babies:
        log_create(db, current_user.id, "DeliveryOutcomeBaby", baby_row.id, get_entity_snapshot(baby_row), ip_address, user_agent)
    log_update(db, current_user.id, "DeliveryReferral", referral.id, old_values, get_entity_snapshot(referral), ip_address, user_agent)

    # Notify sub-centre
    if referral.sub_centre_id:
        sc_users = db.query(User).filter(
            User.sub_centre_id == referral.sub_centre_id,
            User.is_active == True
        ).all()
        if sc_users:
            maternal_flag = "" if outcome_data.maternal_outcome == "healthy" else f" | Maternal outcome: {outcome_data.maternal_outcome.value.replace('_', ' ').title()}"
            NotificationService.create_notification(
                db=db,
                user_ids=[u.id for u in sc_users],
                title="Delivery Outcome Recorded",
                message=f"Delivery outcome for {pw.full_name}: {outcome_data.delivery_type.value.replace('_', ' ').title()}{maternal_flag}",
                notification_type="delivery_outcome_recorded",
                category="delivery",
                priority="high",
                reference_id=outcome.id,
                reference_type="delivery_outcome",
                action_url=f"/delivery-referrals/{referral.id}",
                metadata={
                    "pregnant_woman_name": pw.full_name,
                    "delivery_type": outcome_data.delivery_type.value,
                    "maternal_outcome": outcome_data.maternal_outcome.value,
                }
            )

    return {
        "message": "Delivery outcome recorded successfully",
        "outcome_id": outcome.id,
        "delivery_type": outcome_data.delivery_type.value,
        "baby_count": outcome.baby_count,
        "babies": babies_response,
        "maternal_outcome": outcome_data.maternal_outcome.value,
        "maternal_outcome_notes": outcome_data.maternal_outcome_notes,
        "pregnant_woman_id": pw.id
    }


# ─── Sub-Centre: Record Outcome for Other DP Point referral ─────────────────

@router.post("/{referral_id}/outcome-by-subcentre", response_model=dict)
async def record_outcome_by_subcentre(
    referral_id: int,
    outcome_data: DeliveryOutcomeCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Sub-Centre records delivery outcome for Other DP Point referrals only."""
    if current_user.role != "sub_centre":
        raise HTTPException(status_code=403, detail="Only Sub-Centre users can use this endpoint")

    referral = db.query(DeliveryReferral).filter(
        DeliveryReferral.id == referral_id
    ).first()
    if not referral:
        raise HTTPException(status_code=404, detail="Referral not found")

    # Only the sub-centre that created the referral can record outcome
    if referral.sub_centre_id != current_user.sub_centre_id:
        raise HTTPException(status_code=403, detail="Only the sub-centre that created this referral can record the outcome")

    # Only allowed for Other DP Point referrals
    other_dp_id = get_other_dp_id(db)
    if referral.dp_id != other_dp_id:
        raise HTTPException(status_code=403, detail="Outcome can only be recorded by sub-centre for Other DP Point referrals")

    if referral.status != "accepted":
        raise HTTPException(status_code=400, detail="Referral must be accepted before recording outcome")

    existing_outcome = db.query(DeliveryOutcome).filter(
        DeliveryOutcome.referral_id == referral_id
    ).first()
    if existing_outcome:
        raise HTTPException(status_code=409, detail="Outcome already recorded for this referral")

    other_dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == other_dp_id).first()

    outcome = DeliveryOutcome(
        referral_id=referral_id,
        pregnant_woman_id=referral.pregnant_woman_id,
        dp_id=other_dp_id,
        delivery_type=outcome_data.delivery_type,
        delivery_date=outcome_data.delivery_date,
        baby_count=len(outcome_data.babies) if outcome_data.babies else 0,
        # 'maternal_death' delivery type always implies a maternal death outcome
        maternal_outcome=(
            MaternalOutcome.MATERNAL_DEATH
            if getattr(outcome_data.delivery_type, "value", outcome_data.delivery_type) == "maternal_death"
            else outcome_data.maternal_outcome
        ),
        maternal_outcome_notes=outcome_data.maternal_outcome_notes,
        remarks=outcome_data.remarks,
        recorded_by=current_user.id,
    )
    db.add(outcome)

    old_values = get_entity_snapshot(referral)
    referral.status = "completed"

    pw = db.query(PregnantWoman).filter(PregnantWoman.id == referral.pregnant_woman_id).first()
    pw.pregnancy_outcome = outcome_data.delivery_type.value
    pw.outcome_date = outcome_data.delivery_date.date()
    pw.is_active = False

    db.flush()

    babies_response = []
    if outcome_data.babies:
        for idx, baby in enumerate(outcome_data.babies):
            baby_row = DeliveryOutcomeBaby(
                outcome_id=outcome.id,
                baby_number=idx + 1,
                gender=baby.gender.value,
                status=baby.status,
            )
            db.add(baby_row)
            babies_response.append({"baby_number": idx + 1, "gender": baby.gender.value, "status": baby.status})

    db.commit()
    db.refresh(outcome)

    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "DeliveryOutcome", outcome.id, get_entity_snapshot(outcome), ip_address, user_agent)
    log_update(db, current_user.id, "DeliveryReferral", referral.id, old_values, get_entity_snapshot(referral), ip_address, user_agent)

    # Notify block and district users
    recipient_ids = set()
    if pw.block_id:
        block_users = db.query(User).filter(
            User.block_id == pw.block_id, User.role == "block", User.is_active == True
        ).all()
        recipient_ids.update(u.id for u in block_users)
    if pw.district_id:
        district_users = db.query(User).filter(
            User.district_id == pw.district_id, User.role == "district", User.is_active == True
        ).all()
        recipient_ids.update(u.id for u in district_users)

    if recipient_ids:
        maternal_flag = "" if outcome_data.maternal_outcome == "healthy" else f" | Maternal outcome: {outcome_data.maternal_outcome.value.replace('_', ' ').title()}"
        NotificationService.create_notification(
            db=db,
            user_ids=list(recipient_ids),
            title="Delivery Outcome Recorded",
            message=f"Delivery outcome for {pw.full_name}: {outcome_data.delivery_type.value.replace('_', ' ').title()} (via Other DP Point){maternal_flag}",
            notification_type="delivery_outcome_recorded",
            category="delivery",
            priority="high",
            reference_id=outcome.id,
            reference_type="delivery_outcome",
            action_url="/delivery-referral-management",
            metadata={"pregnant_woman_name": pw.full_name, "delivery_type": outcome_data.delivery_type.value, "maternal_outcome": outcome_data.maternal_outcome.value}
        )

    return {
        "message": "Delivery outcome recorded successfully",
        "outcome_id": outcome.id,
        "delivery_type": outcome_data.delivery_type.value,
        "baby_count": outcome.baby_count,
        "babies": babies_response,
        "maternal_outcome": outcome_data.maternal_outcome.value,
        "maternal_outcome_notes": outcome_data.maternal_outcome_notes,
        "pregnant_woman_id": pw.id
    }


# ─── DP: Discharge (closes the case + schedules PNC reminders) ─────────────

def _create_pnc_reminders(db: Session, discharge: Discharge, pw: PregnantWoman) -> list:
    """Create one PNCReminder per configured PNC visit window, anchored to discharge_date."""
    reminders = []
    for label, days in PNC_REMINDER_OFFSETS:
        reminder = PNCReminder(
            discharge_id=discharge.id,
            pregnant_woman_id=pw.id,
            sub_centre_id=pw.sub_centre_id,
            visit_label=label,
            due_date=(discharge.discharge_date + timedelta(days=days)).date(),
            status=PNCReminderStatus.SCHEDULED,
        )
        db.add(reminder)
        reminders.append(reminder)
    return reminders


@router.post("/{referral_id}/discharge", response_model=dict)
async def discharge_case(
    referral_id: int,
    discharge_data: DischargeCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """DP discharges a completed case — closes it and auto-schedules PNC reminders
    for the ANM/sub-centre (48hr / day 7 / day 42 by default, configurable via
    PNC_REMINDER_OFFSETS)."""
    if current_user.role != "dp":
        raise HTTPException(status_code=403, detail="Only Delivery Point users can discharge a case")

    referral = db.query(DeliveryReferral).filter(
        DeliveryReferral.id == referral_id,
        DeliveryReferral.dp_id == current_user.dp_id
    ).first()
    if not referral:
        raise HTTPException(status_code=404, detail="Referral not found")

    if referral.status != "completed":
        raise HTTPException(status_code=400, detail="A delivery outcome must be recorded before discharge")

    outcome = db.query(DeliveryOutcome).filter(DeliveryOutcome.referral_id == referral_id).first()
    if not outcome:
        raise HTTPException(status_code=400, detail="No delivery outcome found for this referral")

    existing_discharge = db.query(Discharge).filter(Discharge.referral_id == referral_id).first()
    if existing_discharge:
        raise HTTPException(status_code=409, detail="This case has already been discharged")

    pw = db.query(PregnantWoman).filter(PregnantWoman.id == referral.pregnant_woman_id).first()

    discharge = Discharge(
        referral_id=referral_id,
        outcome_id=outcome.id,
        pregnant_woman_id=referral.pregnant_woman_id,
        dp_id=current_user.dp_id,
        discharge_date=discharge_data.discharge_date,
        discharge_facility=discharge_data.discharge_facility,
        discharging_doctor=discharge_data.discharging_doctor,
        condition_at_discharge=discharge_data.condition_at_discharge,
        discharge_notes=discharge_data.discharge_notes,
        discharged_by=current_user.id,
    )
    db.add(discharge)
    db.flush()  # get discharge.id for the PNC reminders

    reminders = _create_pnc_reminders(db, discharge, pw)

    db.commit()
    db.refresh(discharge)

    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "Discharge", discharge.id, get_entity_snapshot(discharge), ip_address, user_agent)
    for reminder in reminders:
        log_create(db, current_user.id, "PNCReminder", reminder.id, get_entity_snapshot(reminder), ip_address, user_agent)

    # Notify sub-centre — case discharged, PNC reminders now on their worklist
    if referral.sub_centre_id:
        sc_users = db.query(User).filter(
            User.sub_centre_id == referral.sub_centre_id,
            User.is_active == True
        ).all()
        if sc_users:
            NotificationService.create_notification(
                db=db,
                user_ids=[u.id for u in sc_users],
                title="Case Discharged — PNC Reminders Scheduled",
                message=f"{pw.full_name} has been discharged. {len(reminders)} PNC reminder(s) added to your worklist.",
                notification_type="discharge_pnc_scheduled",
                category="delivery",
                priority="normal",
                reference_id=discharge.id,
                reference_type="discharge",
                action_url="/pnc-reminders",
                metadata={"pregnant_woman_name": pw.full_name, "referral_id": referral.id}
            )

    return {
        "message": "Case discharged successfully. PNC reminders scheduled.",
        "discharge_id": discharge.id,
        "referral_id": referral.id,
        "pnc_reminders": [
            {"visit_label": r.visit_label, "due_date": r.due_date, "status": r.status}
            for r in reminders
        ],
    }