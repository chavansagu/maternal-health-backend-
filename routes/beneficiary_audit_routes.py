from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from typing import Optional

from database import get_db
from models import (
    PregnantWoman, User, ANCVisit, UserWardMapping, WardSubcentreMapping,
    EDDHistory, USGAppointment, PMSMASession, HRPCondition, HRPStatusLog,
    MobilisationCase, DeliveryReferral, DeliveryOutcome, Admission, Discharge,
    PNCReminder, Ward, Block, District,
)
from auth import get_current_active_user

router = APIRouter(prefix="/beneficiary-audit", tags=["Beneficiary Audit Logs"])

ALLOWED_ROLES = ("district", "block", "sub_centre")


def _scoped_pw_query(db: Session, current_user: User):
    """District: poore district ki PW | Block: apne block ki | Sub-centre: apne area ki."""
    if current_user.role not in ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")

    query = db.query(PregnantWoman)

    if current_user.role == "district":
        return query.filter(PregnantWoman.district_id == current_user.district_id)

    if current_user.role == "block":
        return query.filter(PregnantWoman.block_id == current_user.block_id)

    # sub_centre (same scoping as the Pregnant Women list)
    user_wards = db.query(UserWardMapping.ward_id).filter(
        UserWardMapping.user_id == current_user.id
    ).all()
    if user_wards:
        return query.filter(PregnantWoman.ward_id.in_([w.ward_id for w in user_wards]))

    mapped_ward_ids = db.query(WardSubcentreMapping.ward_id).filter(
        WardSubcentreMapping.sub_centre_id == current_user.sub_centre_id
    ).subquery()
    return query.filter(or_(
        PregnantWoman.ward_id.in_(mapped_ward_ids),
        PregnantWoman.sub_centre_id == current_user.sub_centre_id,
    ))


def _current_stage(pw: PregnantWoman, anc_count: int) -> str:
    if (not pw.is_active) or pw.pregnancy_outcome:
        return "Delivered"
    if anc_count > 0:
        return "ANC"
    return "Registered"


@router.get("/pregnant-women")
async def list_beneficiaries(
    skip: int = 0,
    limit: int = 25,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """PW list for the Beneficiaries Audit Logs page (delivered / inactive women included)."""
    query = _scoped_pw_query(db, current_user)

    if search and search.strip():
        s = f"%{search.strip()}%"
        query = query.filter(or_(
            PregnantWoman.full_name.ilike(s),
            PregnantWoman.mobile_number.ilike(s),
            PregnantWoman.rch_id.ilike(s),
            PregnantWoman.abha_id.ilike(s),
        ))

    total = query.count()
    rows = (
        query.order_by(PregnantWoman.created_at.desc())
        .offset(skip).limit(limit).all()
    )

    pw_ids = [p.id for p in rows]
    anc_counts = {}
    if pw_ids:
        anc_counts = dict(
            db.query(ANCVisit.pregnant_woman_id, func.count(ANCVisit.id))
            .filter(ANCVisit.pregnant_woman_id.in_(pw_ids))
            .group_by(ANCVisit.pregnant_woman_id).all()
        )

    data = [
        {
            "id": p.id,
            "full_name": p.full_name,
            "mobile_number": p.mobile_number,
            "sub_centre_name": p.sub_centre.name if p.sub_centre else None,
            "registered_at": p.created_at,
            "current_stage": _current_stage(p, anc_counts.get(p.id, 0)),
        }
        for p in rows
    ]
    return {"data": data, "total": total}

def _val(x):
    """Enum -> its value, anything else unchanged."""
    return x.value if hasattr(x, "value") else x


def _user_names(db: Session, ids) -> dict:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = db.query(User.id, User.full_name).filter(User.id.in_(ids)).all()
    return {r.id: r.full_name for r in rows}


@router.get("/pregnant-women/{pw_id}/timeline")
async def get_beneficiary_timeline(
    pw_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Registration se delivery/PNC tak ka poora track of one PW. Jis stage ka data nahi, wahan list khali aati hai."""
    pw = _scoped_pw_query(db, current_user).filter(PregnantWoman.id == pw_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")

    anc = db.query(ANCVisit).filter(ANCVisit.pregnant_woman_id == pw_id).order_by(ANCVisit.visit_date, ANCVisit.visit_number).all()
    edd = db.query(EDDHistory).filter(EDDHistory.pregnant_woman_id == pw_id).order_by(EDDHistory.changed_at).all()
    usg = db.query(USGAppointment).filter(USGAppointment.pregnant_woman_id == pw_id).order_by(USGAppointment.scheduled_date).all()
    pmsma = db.query(PMSMASession).filter(PMSMASession.pregnant_woman_id == pw_id).order_by(PMSMASession.scheduled_date).all()
    hrp_conditions = db.query(HRPCondition).filter(HRPCondition.pregnant_woman_id == pw_id).order_by(HRPCondition.identified_at).all()
    hrp_logs = db.query(HRPStatusLog).filter(HRPStatusLog.pregnant_woman_id == pw_id).order_by(HRPStatusLog.changed_at).all()
    mob = db.query(MobilisationCase).filter(MobilisationCase.pregnant_woman_id == pw_id).order_by(MobilisationCase.created_at).all()
    referrals = db.query(DeliveryReferral).filter(DeliveryReferral.pregnant_woman_id == pw_id).order_by(DeliveryReferral.created_at).all()
    admissions = {a.referral_id: a for a in db.query(Admission).filter(Admission.pregnant_woman_id == pw_id).all()}
    outcomes = {o.referral_id: o for o in db.query(DeliveryOutcome).filter(DeliveryOutcome.pregnant_woman_id == pw_id).all()}
    discharges = {d.referral_id: d for d in db.query(Discharge).filter(Discharge.pregnant_woman_id == pw_id).all()}
    pnc = db.query(PNCReminder).filter(PNCReminder.pregnant_woman_id == pw_id).order_by(PNCReminder.due_date).all()

    # one query for every user name used below
    uids = {pw.registered_by}
    uids |= {e.changed_by for e in edd} | {a.attended_by for a in anc}
    uids |= {u.scheduled_by for u in usg} | {u.completed_by for u in usg}
    uids |= {s.scheduled_by for s in pmsma} | {s.completed_by for s in pmsma}
    uids |= {c.identified_by for c in hrp_conditions} | {c.resolved_by for c in hrp_conditions}
    uids |= {l.changed_by for l in hrp_logs} | {m.mobilised_by for m in mob}
    uids |= {r.referred_by_user_id for r in referrals} | {r.accepted_by_user_id for r in referrals}
    uids |= {a.admitted_by for a in admissions.values()} | {o.recorded_by for o in outcomes.values()}
    uids |= {d.discharged_by for d in discharges.values()} | {p.completed_by for p in pnc}
    names = _user_names(db, uids)

    ward = db.query(Ward.name).filter(Ward.id == pw.ward_id).scalar() if pw.ward_id else None
    block = db.query(Block.name).filter(Block.id == pw.block_id).scalar() if pw.block_id else None
    district = db.query(District.name).filter(District.id == pw.district_id).scalar() if pw.district_id else None

    hrp_cols = [c.name for c in PMSMASession.__table__.columns if c.name.startswith("hrp_")]

    return {
        "pregnant_woman": {
            "id": pw.id, "full_name": pw.full_name, "mobile_number": pw.mobile_number,
            "abha_id": pw.abha_id, "rch_id": pw.rch_id, "husband_name": pw.husband_name,
            "age": pw.age, "address": pw.address,
            "ward_name": ward, "sub_centre_name": pw.sub_centre.name if pw.sub_centre else None,
            "block_name": block, "district_name": district,
            "lmp_date": pw.lmp_date, "lmp_edd_date": pw.lmp_edd_date,
            "usg_edd_date": pw.usg_edd_date, "edd_date": pw.edd_date,
            "gravida": pw.gravida, "para": pw.para, "blood_group": pw.blood_group,
            "hrp_status": pw.hrp_status, "is_active": pw.is_active,
            "pregnancy_outcome": pw.pregnancy_outcome, "outcome_date": pw.outcome_date,
            "registered_by": names.get(pw.registered_by), "registered_at": pw.created_at,
        },
        "anc_visits": [{
            "visit_number": a.visit_number, "visit_date": a.visit_date, "weight": a.weight,
            "blood_pressure": a.blood_pressure, "hemoglobin": a.hemoglobin,
            "referred_for_usg": a.referred_for_usg, "is_emergency": a.is_emergency,
            "next_visit_date": a.next_visit_date, "facility_name": a.facility_name,
            "attended_by": names.get(a.attended_by), "recorded_at": a.created_at,
        } for a in anc],
        "edd_history": [{
            "changed_at": e.changed_at, "previous_edd": e.previous_edd, "new_edd": e.new_edd,
            "source": e.source, "changed_by": names.get(e.changed_by),
        } for e in edd],
        "usg_appointments": [{
            "scheduled_date": u.scheduled_date, "usg_centre": u.usg_centre.name if u.usg_centre else None,
            "appointment_type": _val(u.appointment_type), "status": _val(u.status),
            "reschedule_count": u.reschedule_count, "completed_date": u.completed_date,
            "scan_type": u.scan_type, "findings": u.findings,
            "scheduled_by": names.get(u.scheduled_by), "completed_by": names.get(u.completed_by),
        } for u in usg],
        "pmsma_sessions": [{
            "scheduled_date": s.scheduled_date, "visit_number": s.visit_number,
            "visit_type": s.visit_type, "status": s.status, "site": s.pmsma_centre.name if s.pmsma_centre else s.site,
            "visit_date": s.visit_date, "bp": s.bp, "hb": s.hb, "findings": s.findings,
            "hrp_flags": [c[4:].replace("_", " ") for c in hrp_cols if getattr(s, c)],
            "scheduled_by": names.get(s.scheduled_by), "completed_by": names.get(s.completed_by),
        } for s in pmsma],
        "hrp_conditions": [{
            "condition_name": c.condition_name, "source": c.source, "is_active": c.is_active,
            "identified_at": c.identified_at, "identified_by": names.get(c.identified_by),
            "resolved_at": c.resolved_at, "resolved_by": names.get(c.resolved_by), "remarks": c.remarks,
        } for c in hrp_conditions],
        "hrp_status_logs": [{
            "changed_at": l.changed_at, "action": l.action, "old_status": l.old_status,
            "new_status": l.new_status, "note": l.note, "changed_by": names.get(l.changed_by),
        } for l in hrp_logs],
        "mobilisation_cases": [{
            "created_at": m.created_at, "trigger_type": m.trigger_type, "trigger_detail": m.trigger_detail,
            "status": _val(m.status), "priority": m.priority, "escalation_level": m.escalation_level,
            "mobilisation_stage": m.mobilisation_stage, "mobilised_at": m.mobilised_at,
            "mobilised_by": names.get(m.mobilised_by), "remarks": m.remarks,
        } for m in mob],
        "delivery": [{
            "referred_at": r.created_at, "referred_by": names.get(r.referred_by_user_id),
            "delivery_point": r.delivery_point.name if r.delivery_point else None,
            "status": r.status, "cause_of_referral": r.cause_of_referral,
            "accepted_at": r.accepted_at, "accepted_by": names.get(r.accepted_by_user_id),
            "patient_reached_at": r.patient_reached_at,
            "admission": ({
                "admission_date": adm.admission_date, "treating_doctor": adm.treating_doctor,
                "condition_at_admission": adm.condition_at_admission, "admitted_by": names.get(adm.admitted_by),
            } if (adm := admissions.get(r.id)) else None),
            "outcome": ({
                "delivery_date": out.delivery_date, "delivery_type": _val(out.delivery_type),
                "baby_count": out.baby_count, "maternal_outcome": _val(out.maternal_outcome),
                "recorded_by": names.get(out.recorded_by),
            } if (out := outcomes.get(r.id)) else None),
            "discharge": ({
                "discharge_date": dis.discharge_date, "condition_at_discharge": dis.condition_at_discharge,
                "discharged_by": names.get(dis.discharged_by),
            } if (dis := discharges.get(r.id)) else None),
        } for r in referrals],
        "pnc_reminders": [{
            "visit_label": p.visit_label, "due_date": p.due_date, "status": _val(p.status),
            "completed_at": p.completed_at, "completed_by": names.get(p.completed_by), "remarks": p.remarks,
        } for p in pnc],
    }