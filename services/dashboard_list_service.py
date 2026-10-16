"""
Dashboard card -> list behind the number.

`metric` is the card key (same keys as /dashboard/kpi-snapshot). The filter is the SAME
definition the card count uses, so card number == list total.

Every response carries `columns` (what to show, in order) so the screen / mobile app can
render any card's list without knowing the card:
    columns = [{"key": "full_name", "label": "Name", "type": "text"}, ...]   type: text | date | bool
              (optional "detail": true = show behind a "View" button, not in the main row)
    items   = [{"row_id": ..., "full_name": ..., ...}, ...]

Two kinds of metric:
  * women lists   -> a `conditions(today)` function on PregnantWoman (METRICS)
  * case lists    -> a runner function with its own query/columns (RUNNERS), e.g. mobilisation
"""
from datetime import date, datetime, timedelta

from sqlalchemy import and_, case, func, or_, select

from models import (
    Admission, Block, DeliveryOutcome, DeliveryOutcomeBaby, DeliveryPoint, DeliveryReferral,
    DeliveryType, HRPCondition, MaternalOutcome, MobilisationCase, PMSMASession, PregnantWoman as PW,
    SubCentre, USGAppointment, User, WardSubcentreMapping,
)
from services.tracking_service import ENHANCED_FROM_WEEK, FULL_TERM_DAYS, NEAR_EDD_DAYS
from services.dashboard_snapshot_service import (
    _REFERRAL_OPEN, anc_compliance_groups, mobilisation_conditions, pmsma_usg_conditions,
)

class UnknownMetric(ValueError):
    pass


# ── women lists (conditions) ─────────────────────────────────────────────────
def _active(_today):
    # still pregnant: active and no outcome recorded (same as the snapshot card)
    return [PW.is_active == True, PW.pregnancy_outcome.is_(None)]  # noqa: E712


def _hrp(today):
    return _active(today) + [PW.is_high_risk == True]  # noqa: E712


def _weeks_28_plus(today):
    # 28 weeks reached (LMP 28 weeks ago or earlier; no LMP -> EDD within 84 days) - same as the card
    lmp_cut = today - timedelta(weeks=ENHANCED_FROM_WEEK)
    edd_cut = today + timedelta(days=FULL_TERM_DAYS - ENHANCED_FROM_WEEK * 7)
    return _active(today) + [or_(PW.lmp_date <= lmp_cut,
                                 and_(PW.lmp_date.is_(None), PW.edd_date <= edd_cut))]


def _near_edd(today):
    # EDD is 1 to 15 days away (same window the EDD engine uses)
    return _active(today) + [PW.edd_date > today,
                             PW.edd_date <= today + timedelta(days=NEAR_EDD_DAYS)]


def _hrp_near_edd(today):
    return _near_edd(today) + [PW.is_high_risk == True]  # noqa: E712


def _edd_today(today):
    return _active(today) + [PW.edd_date == today]


def _overdue(today):
    # EDD already crossed and still no delivery / outcome recorded (same as the card)
    return _active(today) + [PW.edd_date < today]

def _hrp_by_condition(today):
    return _hrp(today) + [PW.id.in_(
        select(HRPCondition.pregnant_woman_id).where(HRPCondition.is_active == True))]  # noqa: E712


def _hrp_not_mobilised(today):
    return _hrp(today) + [PW.id.in_(
        select(MobilisationCase.pregnant_woman_id)
        .where(MobilisationCase.status.in_(["pending", "escalated"])))]


def _hrp_admitted(today):
    return _hrp(today) + [PW.id.in_(
        select(Admission.pregnant_woman_id).where(Admission.discharge_date.is_(None)))]


def _hrp_referred(today):
    return _hrp(today) + [PW.id.in_(select(DeliveryReferral.pregnant_woman_id))]
METRICS = {
    "active_pw": {"label": "Active PW", "conditions": _active},
    "hrp": {"label": "HRP", "conditions": _hrp},
    "weeks_28_plus": {"label": "28+ Weeks", "conditions": _weeks_28_plus},
    "near_edd": {"label": "Near EDD", "conditions": _near_edd},
    "hrp_near_edd": {"label": "HRP + Near EDD", "conditions": _hrp_near_edd},
    "edd_today": {"label": "EDD Today", "conditions": _edd_today},
    "overdue": {"label": "Overdue", "conditions": _overdue},
    "hrp_by_condition": {"label": "HRP - With Condition", "conditions": _hrp_by_condition},
    "hrp_not_mobilised": {"label": "HRP - Not Mobilised", "conditions": _hrp_not_mobilised},
    "hrp_admitted": {"label": "HRP - Admitted", "conditions": _hrp_admitted},
    "hrp_referred": {"label": "HRP - Referred", "conditions": _hrp_referred},
}
# ── EDD Monitoring panel rows (same buckets as compute_edd_monitoring) ──
EDD_PANEL_BUCKETS = {
    "edd_b_today":   ("EDD Today",        0, 0),
    "edd_b_1_3":     ("EDD in 1-3 days",  1, 3),
    "edd_b_4_7":     ("EDD in 4-7 days",  4, 7),
    "edd_b_8_10":    ("EDD in 8-10 days", 8, 10),
    "edd_b_11_15":   ("EDD in 11-15 days", 11, 15),
    "edd_b_16_30":   ("EDD in 16-30 days", 16, 30),
    "edd_b_30_plus": ("EDD in 30+ days",  31, None),
}


def _edd_bucket(first, last):
    def _conds(today):
        c = _active(today) + [PW.edd_date >= today + timedelta(days=first)]
        if last is not None:
            c.append(PW.edd_date <= today + timedelta(days=last))
        return c
    return _conds


for _key, (_label, _first, _last) in EDD_PANEL_BUCKETS.items():
    METRICS[_key] = {"label": _label, "conditions": _edd_bucket(_first, _last)}
WOMEN_COLUMNS = [
    {"key": "full_name", "label": "Name", "type": "text"},
    {"key": "mobile_number", "label": "Mobile", "type": "text"},
    {"key": "age", "label": "Age", "type": "text"},
    {"key": "block", "label": "Block", "type": "text"},
    {"key": "sub_centre", "label": "Sub-centre", "type": "text"},
    {"key": "edd", "label": "EDD", "type": "date"},
    {"key": "days_left", "label": "Days left", "type": "text"},
    {"key": "is_hrp", "label": "HRP", "type": "bool"},
]


def _search_cond(search):
    like = f"%{search.strip()}%"
    return or_(PW.full_name.ilike(like), PW.mobile_number.ilike(like),
               PW.abha_id.ilike(like), PW.rch_id.ilike(like))


def _sub_centre_name_expr():
    # sub-centre: directly on the woman, else through her ward mapping
    via_ward = (select(SubCentre.name)
                .join(WardSubcentreMapping, WardSubcentreMapping.sub_centre_id == SubCentre.id)
                .where(WardSubcentreMapping.ward_id == PW.ward_id)
                .limit(1).scalar_subquery())
    return func.coalesce(SubCentre.name, via_ward)


def _women_list(db, spec, district_id, block_id, search, skip, limit, today):
    conds = [PW.district_id == district_id, *spec["conditions"](today)]
    if block_id:
        conds.append(PW.block_id == block_id)
    if search and search.strip():
        conds.append(_search_cond(search))

    total = db.query(func.count(PW.id)).filter(*conds).scalar() or 0
    rows = (db.query(PW, Block.name, _sub_centre_name_expr())
            .outerjoin(Block, Block.id == PW.block_id)
            .outerjoin(SubCentre, SubCentre.id == PW.sub_centre_id)
            .filter(*conds)
            .order_by(PW.edd_date.is_(None), PW.edd_date.asc(), PW.id.asc())
            .offset(skip).limit(limit).all())
    items = [{
        "row_id": pw.id,
        "full_name": pw.full_name,
        "mobile_number": pw.mobile_number,
        "age": pw.age,
        "block": block_name,
        "sub_centre": sc_name,
        "edd": pw.edd_date.isoformat() if pw.edd_date else None,
        "days_left": (pw.edd_date - today).days if pw.edd_date else None,
        "is_hrp": bool(pw.is_high_risk),
    } for pw, block_name, sc_name in rows]
    return total, items, WOMEN_COLUMNS


# ── case lists (runners) ─────────────────────────────────────────────────────
_TRIGGER_LABELS = {
    "missed_anc": "Missed ANC", "missed_pmsma": "Missed PMSMA", "missed_usg": "Missed USG",
    "near_edd": "Near EDD", "hrp_unreferred": "HRP not referred",
    "hrp_near_edd": "HRP near EDD", "hrp_near_edd_not_mobilised": "HRP near EDD - not mobilised",
    "edd_today_not_admitted": "EDD today - not admitted",
    "edd_overdue_no_delivery": "EDD crossed - no delivery",
}
_STAGE_LABELS = {
    "pending": "Pending", "contacted": "Contacted", "planned": "Planned",
    "mobilised": "Mobilised", "reached_facility": "Reached Facility", "admitted": "Admitted",
}
_PRIORITY_LABELS = {"critical": "Critical", "red_flag": "Red flag", "high": "High", "normal": "Normal"}

MOBILISATION_COLUMNS = [
    {"key": "full_name", "label": "Name", "type": "text"},
    {"key": "mobile_number", "label": "Mobile", "type": "text"},
    {"key": "block", "label": "Block", "type": "text"},
    {"key": "sub_centre", "label": "Sub-centre", "type": "text"},
    {"key": "edd", "label": "EDD", "type": "date"},
    {"key": "days_left", "label": "Days left", "type": "text"},
    {"key": "is_hrp", "label": "HRP", "type": "bool"},
    {"key": "reason", "label": "Reason", "type": "text", "detail": True},
    {"key": "priority", "label": "Priority", "type": "text", "detail": True},
    {"key": "stage", "label": "Stage", "type": "text", "detail": True},
    {"key": "status", "label": "Status", "type": "text", "detail": True},
    {"key": "pending_action", "label": "Pending action", "type": "text", "detail": True},
]


def _mobilisation_pending(db, district_id, block_id, search, skip, limit, today):
    """One row per open case (status pending / escalated) - the card counts cases."""
    M = MobilisationCase
    conds = [M.district_id == district_id, M.status.in_(["pending", "escalated"])]
    if block_id:
        conds.append(M.block_id == block_id)
    if search and search.strip():
        conds.append(_search_cond(search))

    total = (db.query(func.count(M.id)).join(PW, PW.id == M.pregnant_woman_id)
             .filter(*conds).scalar() or 0)
    order = case((M.priority == "critical", 0), (M.priority == "red_flag", 1),
                 (M.priority == "high", 2), else_=3)
    rows = (db.query(M, PW, Block.name, _sub_centre_name_expr())
            .join(PW, PW.id == M.pregnant_woman_id)
            .outerjoin(Block, Block.id == M.block_id)
            .outerjoin(SubCentre, SubCentre.id == M.sub_centre_id)
            .filter(*conds)
            .order_by(order, PW.edd_date.is_(None), PW.edd_date.asc(), M.id.asc())
            .offset(skip).limit(limit).all())
    items = [{
        "row_id": m.id,
        "pregnant_woman_id": pw.id,
        "full_name": pw.full_name,
        "mobile_number": pw.mobile_number,
        "block": block_name,
        "sub_centre": sc_name,
        "edd": pw.edd_date.isoformat() if pw.edd_date else None,
        "days_left": (pw.edd_date - today).days if pw.edd_date else None,
        "is_hrp": bool(pw.is_high_risk),
        "reason": m.trigger_detail or _TRIGGER_LABELS.get(m.trigger_type, m.trigger_type),
        "priority": _PRIORITY_LABELS.get(m.priority, m.priority),
        "stage": _STAGE_LABELS.get(m.mobilisation_stage, m.mobilisation_stage),
        "status": "Escalated" if getattr(m.status, "value", m.status) == "escalated" else "Pending",
        "pending_action": m.pending_action,
    } for m, pw, block_name, sc_name in rows]
    return total, items, MOBILISATION_COLUMNS


ADMITTED_COLUMNS = [
    {"key": "full_name", "label": "Name", "type": "text"},
    {"key": "mobile_number", "label": "Mobile", "type": "text"},
    {"key": "block", "label": "Block", "type": "text"},
    {"key": "sub_centre", "label": "Sub-centre", "type": "text"},
    {"key": "is_hrp", "label": "HRP", "type": "bool"},
    {"key": "delivery_point", "label": "Delivery point", "type": "text"},
    {"key": "admission_date", "label": "Admitted on", "type": "date"},
    {"key": "days_admitted", "label": "Days admitted", "type": "text"},
    {"key": "edd", "label": "EDD", "type": "date", "detail": True},
    {"key": "days_left", "label": "Days left", "type": "text", "detail": True},
    {"key": "treating_doctor", "label": "Treating doctor", "type": "text", "detail": True},
    {"key": "condition_at_admission", "label": "Condition at admission", "type": "text", "detail": True},
    {"key": "admission_source", "label": "Admission source", "type": "text", "detail": True},
]


def _admitted(db, district_id, block_id, search, skip, limit, today):
    """Currently admitted = admission with no discharge date (the card counts admissions)."""
    A = Admission
    conds = [PW.district_id == district_id, A.discharge_date.is_(None)]
    if block_id:
        conds.append(PW.block_id == block_id)
    if search and search.strip():
        conds.append(_search_cond(search))

    total = (db.query(func.count(A.id)).join(PW, PW.id == A.pregnant_woman_id)
             .filter(*conds).scalar() or 0)
    rows = (db.query(A, PW, Block.name, _sub_centre_name_expr(), DeliveryPoint.name)
            .join(PW, PW.id == A.pregnant_woman_id)
            .outerjoin(Block, Block.id == PW.block_id)
            .outerjoin(SubCentre, SubCentre.id == PW.sub_centre_id)
            .outerjoin(DeliveryPoint, DeliveryPoint.id == A.dp_id)
            .filter(*conds)
            .order_by(A.admission_date.desc(), A.id.desc())
            .offset(skip).limit(limit).all())
    now = datetime.now()
    items = [{
        "row_id": a.id,
        "pregnant_woman_id": pw.id,
        "full_name": pw.full_name,
        "mobile_number": pw.mobile_number,
        "block": block_name,
        "sub_centre": sc_name,
        "is_hrp": bool(pw.is_high_risk),
        "delivery_point": dp_name,
        "admission_date": a.admission_date.isoformat() if a.admission_date else None,
        "days_admitted": max((now - a.admission_date).days, 0) if a.admission_date else None,
        "edd": pw.edd_date.isoformat() if pw.edd_date else None,
        "days_left": (pw.edd_date - today).days if pw.edd_date else None,
        "treating_doctor": a.treating_doctor,
        "condition_at_admission": a.condition_at_admission,
        "admission_source": a.admission_source,
    } for a, pw, block_name, sc_name, dp_name in rows]
    return total, items, ADMITTED_COLUMNS


# ── Referral Pending ─────────────────────────────────────────────────────────
REFERRAL_COLUMNS = [
    {"key": "full_name", "label": "Name", "type": "text"},
    {"key": "mobile_number", "label": "Mobile", "type": "text"},
    {"key": "block", "label": "Block", "type": "text"},
    {"key": "sub_centre", "label": "Sub-centre", "type": "text"},
    {"key": "is_hrp", "label": "HRP", "type": "bool"},
    {"key": "delivery_point", "label": "Referred to", "type": "text"},
    {"key": "referred_on", "label": "Referred on", "type": "date"},
    {"key": "days_pending", "label": "Days pending", "type": "text"},
    {"key": "edd", "label": "EDD", "type": "date", "detail": True},
    {"key": "days_left", "label": "Days left", "type": "text", "detail": True},
    {"key": "cause_of_referral", "label": "Cause of referral", "type": "text", "detail": True},
    {"key": "observation_notes", "label": "Observation notes", "type": "text", "detail": True},
    {"key": "referred_by", "label": "Referred by", "type": "text", "detail": True},
]


def _referral_pending(db, district_id, block_id, search, skip, limit, today):
    R = DeliveryReferral
    conds = [PW.district_id == district_id, R.status == "pending"]
    if block_id:
        conds.append(PW.block_id == block_id)
    if search and search.strip():
        conds.append(_search_cond(search))

    total = (db.query(func.count(R.id)).join(PW, PW.id == R.pregnant_woman_id)
             .filter(*conds).scalar() or 0)
    rows = (db.query(R, PW, Block.name, _sub_centre_name_expr(), DeliveryPoint.name, User.full_name)
            .join(PW, PW.id == R.pregnant_woman_id)
            .outerjoin(Block, Block.id == PW.block_id)
            .outerjoin(SubCentre, SubCentre.id == PW.sub_centre_id)
            .outerjoin(DeliveryPoint, DeliveryPoint.id == R.dp_id)
            .outerjoin(User, User.id == R.referred_by_user_id)
            .filter(*conds)
            .order_by(R.created_at.asc(), R.id.asc())  # waiting longest first
            .offset(skip).limit(limit).all())
    now = datetime.now()
    items = [{
        "row_id": r.id,
        "pregnant_woman_id": pw.id,
        "full_name": pw.full_name,
        "mobile_number": pw.mobile_number,
        "block": block_name,
        "sub_centre": sc_name,
        "is_hrp": bool(pw.is_high_risk),
        "delivery_point": dp_name or r.other_facility_name,
        "referred_on": r.created_at.isoformat() if r.created_at else None,
        "days_pending": max((now - r.created_at).days, 0) if r.created_at else None,
        "edd": pw.edd_date.isoformat() if pw.edd_date else None,
        "days_left": (pw.edd_date - today).days if pw.edd_date else None,
        "cause_of_referral": r.cause_of_referral,
        "observation_notes": r.observation_notes,
        "referred_by": by_name,
    } for r, pw, block_name, sc_name, dp_name, by_name in rows]
    return total, items, REFERRAL_COLUMNS


# ── Maternal Death ───────────────────────────────────────────────────────────
MATERNAL_DEATH_COLUMNS = [
    {"key": "full_name", "label": "Name", "type": "text"},
    {"key": "mobile_number", "label": "Mobile", "type": "text"},
    {"key": "block", "label": "Block", "type": "text"},
    {"key": "sub_centre", "label": "Sub-centre", "type": "text"},
    {"key": "is_hrp", "label": "HRP", "type": "bool"},
    {"key": "delivery_point", "label": "Delivery point", "type": "text"},
    {"key": "delivery_date", "label": "Date", "type": "date"},
    {"key": "delivery_type", "label": "Delivery type", "type": "text", "detail": True},
    {"key": "baby_count", "label": "Babies", "type": "text", "detail": True},
    {"key": "maternal_outcome_notes", "label": "Maternal outcome notes", "type": "text", "detail": True},
    {"key": "remarks", "label": "Remarks", "type": "text", "detail": True},
    {"key": "recorded_by", "label": "Recorded by", "type": "text", "detail": True},
]


def _enum_text(v):
    v = getattr(v, "value", v)
    return str(v).replace("_", " ").capitalize() if v else None


def _maternal_death(db, district_id, block_id, search, skip, limit, today):
    O = DeliveryOutcome
    conds = [PW.district_id == district_id,
             or_(O.maternal_outcome == MaternalOutcome.MATERNAL_DEATH,
                 O.delivery_type == DeliveryType.MATERNAL_DEATH)]
    if block_id:
        conds.append(PW.block_id == block_id)
    if search and search.strip():
        conds.append(_search_cond(search))

    total = (db.query(func.count(O.id)).join(PW, PW.id == O.pregnant_woman_id)
             .filter(*conds).scalar() or 0)
    rows = (db.query(O, PW, Block.name, _sub_centre_name_expr(), DeliveryPoint.name, User.full_name)
            .join(PW, PW.id == O.pregnant_woman_id)
            .outerjoin(Block, Block.id == PW.block_id)
            .outerjoin(SubCentre, SubCentre.id == PW.sub_centre_id)
            .outerjoin(DeliveryPoint, DeliveryPoint.id == O.dp_id)
            .outerjoin(User, User.id == O.recorded_by)
            .filter(*conds)
            .order_by(O.delivery_date.desc(), O.id.desc())
            .offset(skip).limit(limit).all())
    items = [{
        "row_id": o.id,
        "pregnant_woman_id": pw.id,
        "full_name": pw.full_name,
        "mobile_number": pw.mobile_number,
        "block": block_name,
        "sub_centre": sc_name,
        "is_hrp": bool(pw.is_high_risk),
        "delivery_point": dp_name,
        "delivery_date": o.delivery_date.isoformat() if o.delivery_date else None,
        "delivery_type": _enum_text(o.delivery_type),
        "baby_count": o.baby_count,
        "maternal_outcome_notes": o.maternal_outcome_notes,
        "remarks": o.remarks,
        "recorded_by": by_name,
    } for o, pw, block_name, sc_name, dp_name, by_name in rows]
    return total, items, MATERNAL_DEATH_COLUMNS


# ── Neonatal / Infant Death (one row per baby, like the card) ────────────────
INFANT_DEATH_COLUMNS = [
    {"key": "full_name", "label": "Mother", "type": "text"},
    {"key": "mobile_number", "label": "Mobile", "type": "text"},
    {"key": "block", "label": "Block", "type": "text"},
    {"key": "sub_centre", "label": "Sub-centre", "type": "text"},
    {"key": "baby_number", "label": "Baby no.", "type": "text"},
    {"key": "baby_gender", "label": "Gender", "type": "text"},
    {"key": "delivery_point", "label": "Delivery point", "type": "text"},
    {"key": "delivery_date", "label": "Delivery date", "type": "date"},
    {"key": "is_hrp", "label": "Mother HRP", "type": "bool", "detail": True},
    {"key": "delivery_type", "label": "Delivery type", "type": "text", "detail": True},
    {"key": "remarks", "label": "Remarks", "type": "text", "detail": True},
]


def _infant_death(db, district_id, block_id, search, skip, limit, today):
    B, O = DeliveryOutcomeBaby, DeliveryOutcome
    conds = [PW.district_id == district_id, B.status == "infant_death"]
    if block_id:
        conds.append(PW.block_id == block_id)
    if search and search.strip():
        conds.append(_search_cond(search))

    base = (db.query(func.count(B.id)).join(O, O.id == B.outcome_id)
            .join(PW, PW.id == O.pregnant_woman_id))
    total = base.filter(*conds).scalar() or 0
    rows = (db.query(B, O, PW, Block.name, _sub_centre_name_expr(), DeliveryPoint.name)
            .join(O, O.id == B.outcome_id)
            .join(PW, PW.id == O.pregnant_woman_id)
            .outerjoin(Block, Block.id == PW.block_id)
            .outerjoin(SubCentre, SubCentre.id == PW.sub_centre_id)
            .outerjoin(DeliveryPoint, DeliveryPoint.id == O.dp_id)
            .filter(*conds)
            .order_by(O.delivery_date.desc(), B.id.desc())
            .offset(skip).limit(limit).all())
    items = [{
        "row_id": b.id,
        "pregnant_woman_id": pw.id,
        "full_name": pw.full_name,
        "mobile_number": pw.mobile_number,
        "block": block_name,
        "sub_centre": sc_name,
        "baby_number": b.baby_number,
        "baby_gender": _enum_text(b.gender),
        "delivery_point": dp_name,
        "delivery_date": o.delivery_date.isoformat() if o.delivery_date else None,
        "is_hrp": bool(pw.is_high_risk),
        "delivery_type": _enum_text(o.delivery_type),
        "remarks": o.remarks,
    } for b, o, pw, block_name, sc_name, dp_name in rows]
    return total, items, INFANT_DEATH_COLUMNS

# ── HRP Monitoring "By block": one row per block (count of HRP women) ────────
HRP_BY_BLOCK_COLUMNS = [
    {"key": "block", "label": "Block", "type": "text"},
    {"key": "hrp_count", "label": "HRP women", "type": "text"},
]


def _hrp_by_block(db, district_id, block_id, search, skip, limit, today):
    conds = [PW.district_id == district_id, PW.block_id.isnot(None), *_hrp(today)]
    if block_id:
        conds.append(PW.block_id == block_id)
    q = (db.query(PW.block_id, Block.name, func.count(PW.id))
         .outerjoin(Block, Block.id == PW.block_id)
         .filter(*conds)
         .group_by(PW.block_id, Block.name))
    if search and search.strip():
        q = q.filter(Block.name.ilike(f"%{search.strip()}%"))
    rows = q.order_by(func.count(PW.id).desc(), Block.name.asc()).all()
    items = [{"row_id": bid, "block": name or "-", "hrp_count": cnt}
             for bid, name, cnt in rows[skip:skip + limit]]
    return len(rows), items, HRP_BY_BLOCK_COLUMNS
RUNNERS = {
    "mobilisation_pending": {"label": "Mobilisation Pending", "run": _mobilisation_pending},
    "admitted": {"label": "Admitted", "run": _admitted},
    "referral_pending": {"label": "Referral Pending", "run": _referral_pending},
    "maternal_death": {"label": "Maternal Death", "run": _maternal_death},
    "neonatal_infant_death": {"label": "Neonatal / Infant Death", "run": _infant_death},
    "hrp_by_block": {"label": "HRP - By Block", "run": _hrp_by_block},
}

# ── ANC Compliance panel rows (same women as the panel counts) ───────────────
def _anc_row_runner(row_key):
    def _run(db, district_id, block_id, search, skip, limit, today):
        ids = anc_compliance_groups(db, district_id, block_id, today)[row_key]
        spec = {"conditions": lambda _t: [PW.id.in_(ids)]}
        return _women_list(db, spec, district_id, block_id, search, skip, limit, today)
    return _run


for _metric, _row, _label in [
    ("anc_completed",  "completed",  "ANC Completed"),
    ("anc_due",        "due",        "ANC Due"),
    ("anc_overdue",    "overdue",    "ANC Overdue"),
    ("anc_w28_done",   "w28_done",   "ANC Done (28+ wk)"),
    ("anc_w28_missed", "w28_missed", "ANC Missed (28+ wk)"),
]:
    RUNNERS[_metric] = {"label": _label, "run": _anc_row_runner(_row)}
# ── PMSMA / USG panel rows: PMSMA sessions + USG appointments in one list ────
PMSMA_USG_COLUMNS = [
    {"key": "type", "label": "Type", "type": "text"},
    {"key": "full_name", "label": "Name", "type": "text"},
    {"key": "mobile_number", "label": "Mobile", "type": "text"},
    {"key": "block", "label": "Block", "type": "text"},
    {"key": "sub_centre", "label": "Sub-centre", "type": "text"},
    {"key": "scheduled_date", "label": "Scheduled", "type": "date"},
    {"key": "status", "label": "Status", "type": "text"},
    {"key": "is_hrp", "label": "HRP", "type": "bool"},
]


def _pmsma_usg_row_runner(row_key):
    def _run(db, district_id, block_id, search, skip, limit, today):
        pm_conds, us_conds = pmsma_usg_conditions(row_key, today)
        scope = [PW.district_id == district_id]
        if block_id:
            scope.append(PW.block_id == block_id)
        if search and search.strip():
            scope.append(_search_cond(search))

        items = []
        for model, conds, label in ((PMSMASession, pm_conds, "PMSMA"),
                                    (USGAppointment, us_conds, "USG")):
            rows = (db.query(model.id, model.scheduled_date, model.status, PW.full_name,
                             PW.mobile_number, PW.is_high_risk, Block.name, _sub_centre_name_expr())
                    .join(PW, PW.id == model.pregnant_woman_id)
                    .outerjoin(Block, Block.id == PW.block_id)
                    .outerjoin(SubCentre, SubCentre.id == PW.sub_centre_id)
                    .filter(*scope, *conds).all())
            for rid, sdate, status, name, mobile, hrp, block_name, sc_name in rows:
                items.append({
                    "row_id": f"{label.lower()}-{rid}",
                    "type": label,
                    "full_name": name,
                    "mobile_number": mobile,
                    "block": block_name,
                    "sub_centre": sc_name,
                    "scheduled_date": sdate.isoformat() if sdate else None,
                    "status": _enum_text(status),
                    "is_hrp": bool(hrp),
                })
        items.sort(key=lambda i: i["scheduled_date"] or "", reverse=True)
        return len(items), items[skip:skip + limit], PMSMA_USG_COLUMNS
    return _run


for _metric, _row, _label in [
    ("pmsma_usg_scheduled", "scheduled", "PMSMA / USG - Scheduled"),
    ("pmsma_usg_completed", "completed", "PMSMA / USG - Completed"),
    ("pmsma_usg_pending",   "pending",   "PMSMA / USG - Pending"),
    ("pmsma_usg_hrp_found", "hrp_found", "PMSMA / USG - HRP found"),
]:
    RUNNERS[_metric] = {"label": _label, "run": _pmsma_usg_row_runner(_row)}    

# ── Mobilisation & Referral panel rows ───────────────────────────────────────
def _mobilisation_stage_runner(row_key):
    """One row per mobilisation case of the panel row (same columns as Mobilisation Pending)."""
    def _run(db, district_id, block_id, search, skip, limit, today):
        M = MobilisationCase
        conds = [M.district_id == district_id, *mobilisation_conditions(row_key)]
        if block_id:
            conds.append(M.block_id == block_id)
        if search and search.strip():
            conds.append(_search_cond(search))

        total = (db.query(func.count(M.id)).join(PW, PW.id == M.pregnant_woman_id)
                 .filter(*conds).scalar() or 0)
        rows = (db.query(M, PW, Block.name, _sub_centre_name_expr())
                .join(PW, PW.id == M.pregnant_woman_id)
                .outerjoin(Block, Block.id == M.block_id)
                .outerjoin(SubCentre, SubCentre.id == M.sub_centre_id)
                .filter(*conds)
                .order_by(PW.edd_date.is_(None), PW.edd_date.asc(), M.id.asc())
                .offset(skip).limit(limit).all())
        items = [{
            "row_id": m.id,
            "pregnant_woman_id": pw.id,
            "full_name": pw.full_name,
            "mobile_number": pw.mobile_number,
            "block": block_name,
            "sub_centre": sc_name,
            "edd": pw.edd_date.isoformat() if pw.edd_date else None,
            "days_left": (pw.edd_date - today).days if pw.edd_date else None,
            "is_hrp": bool(pw.is_high_risk),
            "reason": m.trigger_detail or _TRIGGER_LABELS.get(m.trigger_type, m.trigger_type),
            "priority": _PRIORITY_LABELS.get(m.priority, m.priority),
            "stage": _STAGE_LABELS.get(m.mobilisation_stage, m.mobilisation_stage),
            "status": _enum_text(m.status),
            "pending_action": m.pending_action,
        } for m, pw, block_name, sc_name in rows]
        return total, items, MOBILISATION_COLUMNS
    return _run


REFERRAL_OPEN_COLUMNS = REFERRAL_COLUMNS[:5] + [
    {"key": "status", "label": "Status", "type": "text"},
] + REFERRAL_COLUMNS[5:]


def _referral_open(db, district_id, block_id, search, skip, limit, today):
    """Referrals still open (pending / accepted), with their status."""
    R = DeliveryReferral
    conds = [PW.district_id == district_id, R.status.in_(_REFERRAL_OPEN)]
    if block_id:
        conds.append(PW.block_id == block_id)
    if search and search.strip():
        conds.append(_search_cond(search))

    total = (db.query(func.count(R.id)).join(PW, PW.id == R.pregnant_woman_id)
             .filter(*conds).scalar() or 0)
    rows = (db.query(R, PW, Block.name, _sub_centre_name_expr(), DeliveryPoint.name, User.full_name)
            .join(PW, PW.id == R.pregnant_woman_id)
            .outerjoin(Block, Block.id == PW.block_id)
            .outerjoin(SubCentre, SubCentre.id == PW.sub_centre_id)
            .outerjoin(DeliveryPoint, DeliveryPoint.id == R.dp_id)
            .outerjoin(User, User.id == R.referred_by_user_id)
            .filter(*conds)
            .order_by(R.created_at.asc(), R.id.asc())
            .offset(skip).limit(limit).all())
    now = datetime.now()
    items = [{
        "row_id": r.id,
        "pregnant_woman_id": pw.id,
        "full_name": pw.full_name,
        "mobile_number": pw.mobile_number,
        "block": block_name,
        "sub_centre": sc_name,
        "is_hrp": bool(pw.is_high_risk),
        "status": _enum_text(r.status),
        "delivery_point": dp_name or r.other_facility_name,
        "referred_on": r.created_at.isoformat() if r.created_at else None,
        "days_pending": max((now - r.created_at).days, 0) if r.created_at else None,
        "edd": pw.edd_date.isoformat() if pw.edd_date else None,
        "days_left": (pw.edd_date - today).days if pw.edd_date else None,
        "cause_of_referral": r.cause_of_referral,
        "observation_notes": r.observation_notes,
        "referred_by": by_name,
    } for r, pw, block_name, sc_name, dp_name, by_name in rows]
    return total, items, REFERRAL_OPEN_COLUMNS


for _metric, _row, _label in [
    ("mob_pending",   "pending",   "Mobilisation - Pending"),
    ("mob_planned",   "planned",   "Mobilisation - Planned"),
    ("mob_mobilised", "mobilised", "Mobilisation - Mobilised"),
]:
    RUNNERS[_metric] = {"label": _label, "run": _mobilisation_stage_runner(_row)}
RUNNERS["referral_open"] = {"label": "Referral Status (open)", "run": _referral_open}
# ── public entry ─────────────────────────────────────────────────────────────
def get_cases(db, metric, district_id, block_id=None, search=None,
              skip=0, limit=25, today=None):
    today = today or date.today()
    if metric in METRICS:
        label = METRICS[metric]["label"]
        total, items, columns = _women_list(db, METRICS[metric], district_id, block_id,
                                            search, skip, limit, today)
    elif metric in RUNNERS:
        label = RUNNERS[metric]["label"]
        total, items, columns = RUNNERS[metric]["run"](db, district_id, block_id,
                                                       search, skip, limit, today)
    else:
        raise UnknownMetric(metric)
    return {"metric": metric, "label": label, "total": total, "skip": skip,
            "limit": limit, "columns": columns, "items": items}