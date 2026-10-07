"""
District Dashboard – Advanced Analytics
=======================================

Every endpoint accepts the same beneficiary filters (see analytics_filters.py)
so the dashboard can apply one filter bar to every widget:

    start_date / end_date            reporting window (events are measured inside it)
    block_id / sub_centre_id / ward_id
    risk, age_band, age_min/age_max, trimester, parity, blood_group,
    registration_source, approval, case_status, risk_factor, anaemic, has_anc

How the filters combine
-----------------------
* The non-date filters define a *cohort* of women.
* The date window is applied to the event each widget measures
  (registration date, ANC visit date, USG scheduled date, delivery date, ...).
* Widgets labelled "now" (active pregnancies, overdue scans, pending referrals)
  are snapshots as of today and ignore the window.

Access: district users only, and always limited to their own district.
"""
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from analytics_filters import (
    AGE_BAND_LABELS, AGE_BANDS, HB_MILD, HB_MODERATE, HB_NORMAL,
    PWFilters, gestational_weeks, pw_conditions, pw_filters_dep,
    resolve_period, trimester_condition, trimester_of,
)
from auth import get_current_active_user
from database import get_db
from models import (
    ANCVisit, Admission, AppointmentStatus, Block, DeliveryOutcome, DeliveryType,
    DeliveryOutcomeBaby, DeliveryPoint, DeliveryReferral, Grievance,
    MaternalOutcome, MobilisationCase, PMSMACentre, PMSMASession, PregnantWoman, SubCentre,
    USGAppointment, USGCentre, User, Ward,
)

router = APIRouter(prefix="/dashboard/district-analytics", tags=["District Analytics"])

PW = PregnantWoman
OPEN_USG = (AppointmentStatus.SCHEDULED, AppointmentStatus.ACCEPTED, AppointmentStatus.RESCHEDULED)


# ── small helpers ────────────────────────────────────────────────────────────

def _guard(user: User) -> User:
    if user.role != "district" or user.district_id is None:
        raise HTTPException(status_code=403, detail="District user access required")
    return user


def _i(v) -> int:
    return int(v or 0)


def _pct(n, d) -> float:
    return round(n / d * 100, 1) if d else 0.0


def _ev(v):
    """Enum member -> plain value (works for str-Enums and plain strings)."""
    return getattr(v, "value", v)


def _to_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _start_dt(d: date) -> datetime:
    return datetime.combine(d, time.min)


def _end_dt(d: date) -> datetime:
    """Exclusive upper bound for a DateTime column covering all of day `d`."""
    return datetime.combine(d + timedelta(days=1), time.min)


def _delta(cur, prev, unit="count"):
    if prev is None:
        return {"change": None, "change_type": None}
    if unit == "percent":
        return {"change": round(cur - prev, 1), "change_type": "pts"}
    if not prev:
        return {"change": None, "change_type": "pct"}
    return {"change": round((cur - prev) / prev * 100, 1), "change_type": "pct"}


def _cohort(db: Session, f: PWFilters, user: User) -> list:
    return pw_conditions(f, user)


def _grievance_conds(f: PWFilters, user: User) -> list:
    """Grievances can only be narrowed geographically (they have no risk/age data)."""
    conds = [Grievance.district_id == user.district_id]
    if f.block_id:
        conds.append(Grievance.block_id == f.block_id)
    if f.ward_id:
        conds.append(Grievance.ward_id == f.ward_id)
    return conds


def _name_map(db: Session, model, ids) -> Dict[int, str]:
    ids = [i for i in set(ids) if i is not None]
    if not ids:
        return {}
    return {r.id: r.name for r in db.query(model.id, model.name).filter(model.id.in_(ids)).all()}


# Canonical human-readable labels for known risk-factor categories. Matching is
# case-insensitive against the raw text *after* the "Auto-detected:" prefix and
# any trailing parenthetical reading (e.g. "(111/95)", "(95.0 kg)") have been
# stripped. Anything not found here falls back to a title-cased version of the
# cleaned text, so new auto-detected reasons (e.g. "High Weight") still show up
# sensibly without needing a map entry.
RISK_FACTOR_LABEL_MAP = {
    "high bp": "High Blood Pressure",
    "low bp": "Low Blood Pressure",
    "low fetal heart rate": "Low Fetal Heart Rate",
    "hypertension": "Hypertension",
    "diabetes": "Diabetes",
    "anaemia": "Anaemia",
    "thyroid disorder": "Thyroid Disorder",
    "previous c-section": "Previous C-Section",
    "multiple pregnancy": "Multiple Pregnancy",
}

# Never show these as a "risk factor" bar — they're filter/detail fields only.
RISK_FACTOR_EXCLUDED = {"blood group"}

_RISK_FACTOR_PREFIX_RE = re.compile(r"^\s*auto-detected\s*:\s*", re.IGNORECASE)
_RISK_FACTOR_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _normalize_risk_factor(raw: str) -> Optional[dict]:
    """Turn a raw risk-factor fragment into a normalized {key, label} pair.

    Strips the "Auto-detected:" prefix and any trailing parenthetical reading
    (e.g. "High BP (111/95)" -> "High BP"), then maps to a human-readable
    label via RISK_FACTOR_LABEL_MAP, falling back to title-case. Returns None
    for fragments that should never appear as a risk-factor category (e.g.
    "Blood Group").
    """
    cleaned = _RISK_FACTOR_PAREN_RE.sub("", _RISK_FACTOR_PREFIX_RE.sub("", raw)).strip()
    if not cleaned:
        return None
    key = cleaned.lower()
    if key in RISK_FACTOR_EXCLUDED:
        return None
    label = RISK_FACTOR_LABEL_MAP.get(key, cleaned.title() if cleaned.islower() else cleaned)
    return {"key": key, "label": label}


def _person(pw: PregnantWoman, blocks: Dict[int, str], subs: Dict[int, str]) -> dict:
    return {
        "id": pw.id,
        "name": pw.full_name,
        "mobile": pw.mobile_number,
        "age": pw.age,
        "block": blocks.get(pw.block_id),
        "sub_centre": subs.get(pw.sub_centre_id),
        "edd": pw.edd_date,
        "is_high_risk": bool(pw.is_high_risk),
        "risk_factors": pw.risk_factors,
    }


def _action_status(db: Session, pw_ids, trigger_types) -> Dict[int, dict]:
    """Follow-up status per woman, taken from the mobilisation worklist.

    Looks at the most recent mobilisation case for each woman whose trigger is in
    `trigger_types`. Women with no such case are simply absent from the result
    (the caller treats that as "no action yet").
    """
    pw_ids = [i for i in set(pw_ids) if i is not None]
    if not pw_ids:
        return {}
    rows = db.query(MobilisationCase).filter(
        MobilisationCase.pregnant_woman_id.in_(pw_ids),
        MobilisationCase.trigger_type.in_(list(trigger_types)),
    ).order_by(MobilisationCase.created_at.asc()).all()
    out: Dict[int, dict] = {}
    for c in rows:  # ascending, so the latest case wins
        out[c.pregnant_woman_id] = {"key": _ev(c.status), "detail": c.escalation_level if _ev(c.status) == "escalated" else None}
    return out


def _with_action(item: dict, actions: Dict[int, dict]) -> dict:
    item["action_status"] = actions.get(item["id"]) or {"key": "none", "detail": None}
    return item


# ─────────────────────────────────────────────────────────────────────────────
# 1. Filter options (drives the cascading dropdowns)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/filter-options")
async def filter_options(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    _guard(user)
    blocks = db.query(Block).filter(Block.district_id == user.district_id, Block.is_active == True).order_by(Block.name).all()  # noqa: E712
    block_ids = [b.id for b in blocks]
    subs = db.query(SubCentre).filter(SubCentre.block_id.in_(block_ids), SubCentre.is_active == True).order_by(SubCentre.name).all() if block_ids else []  # noqa: E712
    wards = db.query(Ward).filter(Ward.block_id.in_(block_ids), Ward.is_active == True).order_by(Ward.name).all() if block_ids else []  # noqa: E712
    usg = db.query(USGCentre).filter(USGCentre.district_id == user.district_id, USGCentre.is_active == True).order_by(USGCentre.name).all()  # noqa: E712
    dps = db.query(DeliveryPoint).filter(DeliveryPoint.district_id == user.district_id, DeliveryPoint.is_active == True).order_by(DeliveryPoint.name).all()  # noqa: E712

    groups = [r[0] for r in db.query(PW.blood_group).filter(
        PW.district_id == user.district_id, PW.blood_group.isnot(None), PW.blood_group != ""
    ).distinct().all()]

    rf_counter: Counter = Counter()
    for (text,) in db.query(PW.risk_factors).filter(
        PW.district_id == user.district_id, PW.risk_factors.isnot(None), PW.risk_factors != ""
    ).all():
        for part in text.split(","):
            part = part.strip()
            if part:
                rf_counter[part.title() if part.islower() else part] += 1

    return {
        "blocks": [{"id": b.id, "name": b.name} for b in blocks],
        "sub_centres": [{"id": s.id, "name": s.name, "block_id": s.block_id} for s in subs],
        "wards": [{"id": w.id, "name": w.name, "block_id": w.block_id} for w in wards],
        "usg_centres": [{"id": c.id, "name": c.name} for c in usg],
        "delivery_points": [{"id": d.id, "name": d.name} for d in dps],
        "blood_groups": sorted(groups),
        "risk_factors": [{"name": k, "count": v} for k, v in rf_counter.most_common(40)],
        "age_bands": [{"key": k, "label": v} for k, v in AGE_BAND_LABELS.items()],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Summary KPIs with previous-period comparison
# ─────────────────────────────────────────────────────────────────────────────

def _period_metrics(db: Session, conds: list, gconds: list, s: date, e: date) -> dict:
    """Everything that is measured *inside* a date window."""
    m: dict = {}

    reg = db.query(func.count(PW.id), func.sum(case((PW.is_high_risk == True, 1), else_=0))).filter(  # noqa: E712
        *conds, PW.created_at >= _start_dt(s), PW.created_at < _end_dt(e)).one()
    m["registrations"], m["hr_registrations"] = _i(reg[0]), _i(reg[1])

    anc = db.query(func.count(ANCVisit.id), func.count(func.distinct(ANCVisit.pregnant_woman_id))) \
        .join(PW, PW.id == ANCVisit.pregnant_woman_id) \
        .filter(*conds, ANCVisit.visit_date >= s, ANCVisit.visit_date <= e).one()
    m["anc_visits"], m["women_with_anc"] = _i(anc[0]), _i(anc[1])

    usg = db.query(func.count(USGAppointment.id),
                   func.sum(case((USGAppointment.status == AppointmentStatus.COMPLETED, 1), else_=0))) \
        .join(PW, PW.id == USGAppointment.pregnant_woman_id) \
        .filter(*conds, USGAppointment.scheduled_date >= _start_dt(s), USGAppointment.scheduled_date < _end_dt(e)).one()
    m["usg_total"], m["usg_completed"] = _i(usg[0]), _i(usg[1])

    m["referrals"] = _i(db.query(func.count(DeliveryReferral.id)).join(PW, PW.id == DeliveryReferral.pregnant_woman_id)
                        .filter(*conds, DeliveryReferral.created_at >= _start_dt(s), DeliveryReferral.created_at < _end_dt(e)).scalar())

    out_f = [DeliveryOutcome.delivery_date >= _start_dt(s), DeliveryOutcome.delivery_date < _end_dt(e)]
    m["deliveries"] = _i(db.query(func.count(DeliveryOutcome.id)).join(PW, PW.id == DeliveryOutcome.pregnant_woman_id)
                         .filter(*conds, *out_f).scalar())
    m["maternal_deaths"] = _i(db.query(func.count(DeliveryOutcome.id)).join(PW, PW.id == DeliveryOutcome.pregnant_woman_id)
                              .filter(*conds, *out_f, or_(DeliveryOutcome.maternal_outcome == MaternalOutcome.MATERNAL_DEATH,
                                                          DeliveryOutcome.delivery_type == DeliveryType.MATERNAL_DEATH)).scalar())
    babies = db.query(DeliveryOutcomeBaby.status, func.count(DeliveryOutcomeBaby.id)) \
        .join(DeliveryOutcome, DeliveryOutcome.id == DeliveryOutcomeBaby.outcome_id) \
        .join(PW, PW.id == DeliveryOutcome.pregnant_woman_id) \
        .filter(*conds, *out_f).group_by(DeliveryOutcomeBaby.status).all()
    bmap = {k: _i(v) for k, v in babies}
    m["infant_deaths"], m["still_births"] = bmap.get("infant_death", 0), bmap.get("still_birth", 0)
    m["live_births"] = bmap.get("live_birth", 0)

    g = db.query(func.count(Grievance.id), func.sum(case((Grievance.status == "resolved", 1), else_=0))) \
        .filter(*gconds, Grievance.created_at >= _start_dt(s), Grievance.created_at < _end_dt(e)).one()
    m["grievances"], m["grievances_resolved"] = _i(g[0]), _i(g[1])
    return m


@router.get("/summary")
async def summary(
    f: PWFilters = Depends(pw_filters_dep),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    _guard(user)
    s, e, ps, pe = resolve_period(f.start_date, f.end_date)
    conds, gconds = _cohort(db, f, user), _grievance_conds(f, user)
    cur = _period_metrics(db, conds, gconds, s, e)
    prev = _period_metrics(db, conds, gconds, ps, pe)

    today = date.today()
    active = db.query(func.count(PW.id)).filter(*conds, PW.is_active == True).scalar() or 0  # noqa: E712
    active_hr = db.query(func.count(PW.id)).filter(*conds, PW.is_active == True, PW.is_high_risk == True).scalar() or 0  # noqa: E712
    no_anc = db.query(func.count(PW.id)).filter(
        *conds, PW.is_active == True,  # noqa: E712
        ~exists().where(ANCVisit.pregnant_woman_id == PW.id).correlate(PW),
    ).scalar() or 0
    overdue_usg = db.query(func.count(USGAppointment.id)).join(PW, PW.id == USGAppointment.pregnant_woman_id).filter(
        *conds, USGAppointment.status.in_(OPEN_USG), USGAppointment.scheduled_date < _start_dt(today)).scalar() or 0
    pending_ref = db.query(func.count(DeliveryReferral.id)).join(PW, PW.id == DeliveryReferral.pregnant_woman_id).filter(
        *conds, DeliveryReferral.status == "pending").scalar() or 0
    open_g = db.query(func.count(Grievance.id)).filter(*gconds, Grievance.status != "resolved").scalar() or 0

    def rate(d, num, den):
        return _pct(d[num], d[den])

    kpis: List[dict] = []

    def add(key, label, value, previous=None, unit="count", good="up", hint=None, group="period"):
        kpis.append({"key": key, "label": label, "value": value, "previous": previous, "unit": unit,
                     "good": good, "hint": hint, "group": group, **_delta(value, previous, unit)})

    add("registrations", "New registrations", cur["registrations"], prev["registrations"], hint="Women registered in the selected period")
    add("hr_share", "High-risk share of registrations", rate(cur, "hr_registrations", "registrations"), rate(prev, "hr_registrations", "registrations"), unit="percent", good="down")
    add("anc_visits", "ANC visits", cur["anc_visits"], prev["anc_visits"], hint=f"{cur['women_with_anc']} women seen")
    add("usg_total", "USG appointments", cur["usg_total"], prev["usg_total"])
    add("usg_completion", "USG completion rate", rate(cur, "usg_completed", "usg_total"), rate(prev, "usg_completed", "usg_total"), unit="percent", hint="Of appointments scheduled in the period")
    add("referrals", "Delivery referrals", cur["referrals"], prev["referrals"])
    add("deliveries", "Deliveries recorded", cur["deliveries"], prev["deliveries"])
    add("maternal_deaths", "Maternal deaths", cur["maternal_deaths"], prev["maternal_deaths"], good="down")
    add("infant_deaths", "Infant deaths", cur["infant_deaths"], prev["infant_deaths"], good="down")
    add("still_births", "Still births", cur["still_births"], prev["still_births"], good="down")
    add("grievances", "Grievances raised", cur["grievances"], prev["grievances"], good="down", hint="Geography filters only")
    add("grievance_resolution", "Grievance resolution rate", rate(cur, "grievances_resolved", "grievances"), rate(prev, "grievances_resolved", "grievances"), unit="percent", hint="Geography filters only")

    add("active", "Active pregnancies", active, group="now", hint="As of today")
    add("active_hr", "Active high-risk", active_hr, group="now", good="down")
    add("no_anc", "Active women with no ANC visit", no_anc, group="now", good="down", hint="Registered but never seen for ANC")
    add("overdue_usg", "Overdue USG", overdue_usg, group="now", good="down", hint="Scheduled date passed, scan not done")
    add("pending_referrals", "Pending delivery referrals", pending_ref, group="now", good="down")
    add("open_grievances", "Open grievances", open_g, group="now", good="down")

    return {
        "period": {"start_date": s, "end_date": e, "previous_start": ps, "previous_end": pe, "days": (e - s).days + 1},
        "filters": f.describe(),
        "kpis": kpis,
        "notes": ["Grievances can only be narrowed by block/ward; other filters do not apply to them."],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. Trends (day / week / month buckets)
# ─────────────────────────────────────────────────────────────────────────────

def _bucket_start(d: date, gran: str) -> date:
    if gran == "week":
        return d - timedelta(days=d.weekday())          # Monday
    if gran == "month":
        return d.replace(day=1)
    return d


def _next_bucket(d: date, gran: str) -> date:
    if gran == "week":
        return d + timedelta(days=7)
    if gran == "month":
        return (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return d + timedelta(days=1)


def _bucket_label(d: date, gran: str) -> str:
    if gran == "month":
        return d.strftime("%b %Y")
    if gran == "week":
        return "Wk " + d.strftime("%d %b")
    return d.strftime("%d %b")


@router.get("/trends")
async def trends(
    granularity: str = Query("auto", description="auto | day | week | month"),
    f: PWFilters = Depends(pw_filters_dep),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    _guard(user)
    s, e, _, _ = resolve_period(f.start_date, f.end_date)
    days = (e - s).days + 1
    gran = granularity if granularity in ("day", "week", "month") else ("day" if days <= 45 else "week" if days <= 180 else "month")
    conds, gconds = _cohort(db, f, user), _grievance_conds(f, user)

    metrics = ["registrations", "hr_registrations", "anc_visits", "usg_scheduled", "usg_completed",
               "referrals", "deliveries", "grievances"]
    data: Dict[date, Dict[str, int]] = defaultdict(lambda: {k: 0 for k in metrics})

    def put(rows, key):
        for row in rows:
            d = _to_date(row[0])
            if d is None:
                continue
            data[_bucket_start(d, gran)][key] += _i(row[1])

    reg_day = func.date(PW.created_at)
    for d, tot, hr in db.query(reg_day, func.count(PW.id), func.sum(case((PW.is_high_risk == True, 1), else_=0))).filter(  # noqa: E712
            *conds, PW.created_at >= _start_dt(s), PW.created_at < _end_dt(e)).group_by(reg_day).all():
        b = _bucket_start(_to_date(d), gran)
        data[b]["registrations"] += _i(tot)
        data[b]["hr_registrations"] += _i(hr)

    put(db.query(ANCVisit.visit_date, func.count(ANCVisit.id)).join(PW, PW.id == ANCVisit.pregnant_woman_id)
        .filter(*conds, ANCVisit.visit_date >= s, ANCVisit.visit_date <= e).group_by(ANCVisit.visit_date).all(), "anc_visits")

    usg_day = func.date(USGAppointment.scheduled_date)
    for d, tot, done in db.query(usg_day, func.count(USGAppointment.id),
                                 func.sum(case((USGAppointment.status == AppointmentStatus.COMPLETED, 1), else_=0))) \
            .join(PW, PW.id == USGAppointment.pregnant_woman_id) \
            .filter(*conds, USGAppointment.scheduled_date >= _start_dt(s), USGAppointment.scheduled_date < _end_dt(e)) \
            .group_by(usg_day).all():
        b = _bucket_start(_to_date(d), gran)
        data[b]["usg_scheduled"] += _i(tot)
        data[b]["usg_completed"] += _i(done)

    ref_day = func.date(DeliveryReferral.created_at)
    put(db.query(ref_day, func.count(DeliveryReferral.id)).join(PW, PW.id == DeliveryReferral.pregnant_woman_id)
        .filter(*conds, DeliveryReferral.created_at >= _start_dt(s), DeliveryReferral.created_at < _end_dt(e))
        .group_by(ref_day).all(), "referrals")

    del_day = func.date(DeliveryOutcome.delivery_date)
    put(db.query(del_day, func.count(DeliveryOutcome.id)).join(PW, PW.id == DeliveryOutcome.pregnant_woman_id)
        .filter(*conds, DeliveryOutcome.delivery_date >= _start_dt(s), DeliveryOutcome.delivery_date < _end_dt(e))
        .group_by(del_day).all(), "deliveries")

    grv_day = func.date(Grievance.created_at)
    put(db.query(grv_day, func.count(Grievance.id)).filter(
        *gconds, Grievance.created_at >= _start_dt(s), Grievance.created_at < _end_dt(e)).group_by(grv_day).all(), "grievances")

    buckets, cur = [], _bucket_start(s, gran)
    while cur <= e:
        row = data.get(cur, {k: 0 for k in metrics})
        buckets.append({"bucket": cur, "label": _bucket_label(cur, gran), **row,
                        "hr_share": _pct(row["hr_registrations"], row["registrations"]),
                        "usg_completion": _pct(row["usg_completed"], row["usg_scheduled"])})
        cur = _next_bucket(cur, gran)

    totals = {k: sum(b[k] for b in buckets) for k in metrics}
    return {"granularity": gran, "period": {"start_date": s, "end_date": e, "days": days},
            "series": buckets, "totals": totals, "filters": f.describe()}


# ─────────────────────────────────────────────────────────────────────────────
# 4. Demographic & risk profile
# ─────────────────────────────────────────────────────────────────────────────

def _age_band_key(age: Optional[int]) -> str:
    if age is None:
        return "unknown"
    for key, (lo, hi) in AGE_BANDS.items():
        if (lo is None or age >= lo) and (hi is None or age <= hi):
            return key
    return "unknown"


@router.get("/demographics")
async def demographics(
    population: str = Query("period", description="period = registered in window, all = every woman in the filter"),
    f: PWFilters = Depends(pw_filters_dep),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    _guard(user)
    s, e, _, _ = resolve_period(f.start_date, f.end_date)
    conds = _cohort(db, f, user)
    pop = list(conds)
    if population != "all":
        pop += [PW.created_at >= _start_dt(s), PW.created_at < _end_dt(e)]

    total = db.query(func.count(PW.id)).filter(*pop).scalar() or 0
    hr_total = db.query(func.count(PW.id)).filter(*pop, PW.is_high_risk == True).scalar() or 0  # noqa: E712

    # Age bands (+ risk share within each band)
    bands = {k: {"key": k, "label": AGE_BAND_LABELS[k], "total": 0, "high_risk": 0} for k in AGE_BAND_LABELS}
    for age, hr, n in db.query(PW.age, PW.is_high_risk, func.count(PW.id)).filter(*pop).group_by(PW.age, PW.is_high_risk).all():
        b = bands[_age_band_key(age)]
        b["total"] += _i(n)
        if hr:
            b["high_risk"] += _i(n)
    age_rows = [{**b, "hr_pct": _pct(b["high_risk"], b["total"])} for b in bands.values() if b["total"]]

    # Parity
    par = {"primi": ["First pregnancy", 0, 0], "multi": ["2nd–3rd pregnancy", 0, 0],
           "grand": ["4th or more", 0, 0], "unknown": ["Not recorded", 0, 0]}
    for g, hr, n in db.query(PW.gravida, PW.is_high_risk, func.count(PW.id)).filter(*pop).group_by(PW.gravida, PW.is_high_risk).all():
        k = "unknown" if g is None else "primi" if g <= 1 else "multi" if g <= 3 else "grand"
        par[k][1] += _i(n)
        if hr:
            par[k][2] += _i(n)
    parity_rows = [{"key": k, "label": v[0], "total": v[1], "high_risk": v[2], "hr_pct": _pct(v[2], v[1])} for k, v in par.items() if v[1]]

    # Blood group
    bg_rows = [{"label": bg or "Not recorded", "count": _i(n)} for bg, n in
               db.query(PW.blood_group, func.count(PW.id)).filter(*pop).group_by(PW.blood_group).order_by(func.count(PW.id).desc()).all()]

    # Registration source & approval
    src = {"self": 0, "staff": 0}
    for flag, n in db.query(PW.is_self_registered, func.count(PW.id)).filter(*pop).group_by(PW.is_self_registered).all():
        src["self" if flag else "staff"] += _i(n)
    appr = {"approved": 0, "pending": 0}
    for flag, n in db.query(PW.registration_approved, func.count(PW.id)).filter(*pop).group_by(PW.registration_approved).all():
        appr["approved" if flag else "pending"] += _i(n)

    # Current gestational stage of ongoing pregnancies in the filter
    stage_base = [c for c in conds] + [PW.is_active == True]  # noqa: E712
    stage = {}
    for t in ("1", "2", "3", "unknown"):
        cond = trimester_condition([t])
        stage[t] = db.query(func.count(PW.id)).filter(*stage_base, cond).scalar() or 0
    stage_rows = [
        {"key": "1", "label": "1st trimester (<14 wks)", "count": stage["1"]},
        {"key": "2", "label": "2nd trimester (14–27 wks)", "count": stage["2"]},
        {"key": "3", "label": "3rd trimester (28+ wks)", "count": stage["3"]},
        {"key": "unknown", "label": "LMP / EDD not recorded", "count": stage["unknown"]},
    ]

    # Gestational age at registration (early registration = before 12 weeks)
    timing = Counter()
    for lmp, edd, created in db.query(PW.lmp_date, PW.edd_date, PW.created_at).filter(*pop).all():
        ref = lmp or (edd - timedelta(days=280) if edd else None)
        if not ref or not created:
            timing["unknown"] += 1
            continue
        wk = max((_to_date(created) - ref).days // 7, 0)
        timing["early" if wk < 12 else "mid" if wk < 28 else "late"] += 1
    known = timing["early"] + timing["mid"] + timing["late"]

    # Risk factors (a woman can carry several). Raw fragments look like
    # "Auto-detected: High BP (140/82)" or plain "High Weight (95.0 kg)" once
    # split off a comma-separated list — normalize each into a human-readable
    # category, summing counts for fragments that collapse into the same
    # category, while keeping a few raw examples for a tooltip.
    rf: Dict[str, dict] = {}
    rf_women = 0
    for (text,) in db.query(PW.risk_factors).filter(*pop, PW.risk_factors.isnot(None), PW.risk_factors != "").all():
        rf_women += 1
        for part in {p.strip() for p in text.split(",") if p.strip()}:
            norm = _normalize_risk_factor(part)
            if norm is None:
                continue
            entry = rf.setdefault(norm["key"], {"name": norm["label"], "count": 0, "raw_examples": []})
            entry["count"] += 1
            if part not in entry["raw_examples"] and len(entry["raw_examples"]) < 3:
                entry["raw_examples"].append(part)
    rf_rows = sorted(
        ({"name": v["name"], "count": v["count"], "share": _pct(v["count"], rf_women), "raw_examples": v["raw_examples"]}
         for v in rf.values()),
        key=lambda r: -r["count"])[:15]

    return {
        "population": "all" if population == "all" else "period",
        "period": {"start_date": s, "end_date": e},
        "total_women": total, "high_risk": hr_total, "hr_pct": _pct(hr_total, total),
        "age_bands": age_rows, "parity": parity_rows, "blood_groups": bg_rows,
        "registration_source": [{"label": "Registered by staff", "count": src["staff"]}, {"label": "Self-registered", "count": src["self"]}],
        "approval": [{"label": "Approved", "count": appr["approved"]}, {"label": "Pending approval", "count": appr["pending"]}],
        "current_stage": stage_rows,
        "registration_timing": {
            "early": timing["early"], "mid": timing["mid"], "late": timing["late"], "unknown": timing["unknown"],
            "early_pct": _pct(timing["early"], known),
        },
        "risk_factors": rf_rows,
        "filters": f.describe(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. Care cascade – ANC coverage, anaemia, USG performance
# ─────────────────────────────────────────────────────────────────────────────

# Minimum ANC visits a woman *currently* in that trimester should have had by now.
EXPECTED_ANC_BY_TRIMESTER = {"1": 1, "2": 2, "3": 3}


def _visit_counts_subquery(db: Session, upto: date):
    return db.query(
        ANCVisit.pregnant_woman_id.label("pid"),
        func.count(ANCVisit.id).label("n"),
        func.max(ANCVisit.visit_date).label("last"),
    ).filter(ANCVisit.visit_date <= upto).group_by(ANCVisit.pregnant_woman_id).subquery()


@router.get("/care")
async def care(
    f: PWFilters = Depends(pw_filters_dep),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    _guard(user)
    s, e, _, _ = resolve_period(f.start_date, f.end_date)
    conds = _cohort(db, f, user)
    today = date.today()

    # ---- ANC coverage funnel: women registered by the end date, visits counted up to the end date
    vc = _visit_counts_subquery(db, e)
    n = func.coalesce(vc.c.n, 0)

    def ge(k):
        return func.sum(case((n >= k, 1), else_=0))

    tot, a1, a2, a3, a4, avg_n = db.query(func.count(PW.id), ge(1), ge(2), ge(3), ge(4), func.avg(n)) \
        .select_from(PW).outerjoin(vc, vc.c.pid == PW.id).filter(*conds, PW.created_at < _end_dt(e)).one()
    tot, a1, a2, a3, a4 = _i(tot), _i(a1), _i(a2), _i(a3), _i(a4)
    funnel = [
        {"label": "Registered", "count": tot, "pct": 100.0 if tot else 0.0},
        {"label": "1+ ANC visit", "count": a1, "pct": _pct(a1, tot)},
        {"label": "2+ ANC visits", "count": a2, "pct": _pct(a2, tot)},
        {"label": "3+ ANC visits", "count": a3, "pct": _pct(a3, tot)},
        {"label": "4+ ANC visits", "count": a4, "pct": _pct(a4, tot)},
    ]

    # ---- On-track check for ongoing pregnancies, by current trimester
    on_track = []
    labels = {"1": "1st trimester", "2": "2nd trimester", "3": "3rd trimester"}
    for t, need in EXPECTED_ANC_BY_TRIMESTER.items():
        cnt, ok, avg_v = db.query(func.count(PW.id), func.sum(case((n >= need, 1), else_=0)), func.avg(n)) \
            .select_from(PW).outerjoin(vc, vc.c.pid == PW.id) \
            .filter(*conds, PW.is_active == True, trimester_condition([t])).one()  # noqa: E712
        on_track.append({"trimester": t, "label": labels[t], "expected_visits": need, "women": _i(cnt),
                         "on_track": _i(ok), "behind": _i(cnt) - _i(ok), "on_track_pct": _pct(_i(ok), _i(cnt)),
                         "avg_visits": round(float(avg_v or 0), 1)})

    # ---- Anaemia: latest Hb on record for each ongoing pregnancy
    latest = db.query(ANCVisit.pregnant_woman_id.label("pid"), func.max(ANCVisit.visit_date).label("d")) \
        .filter(ANCVisit.hemoglobin.isnot(None), ANCVisit.visit_date <= e).group_by(ANCVisit.pregnant_woman_id).subquery()
    hb_sub = db.query(ANCVisit.pregnant_woman_id.label("pid"), func.min(ANCVisit.hemoglobin).label("hb")) \
        .join(latest, and_(latest.c.pid == ANCVisit.pregnant_woman_id, latest.c.d == ANCVisit.visit_date)) \
        .filter(ANCVisit.hemoglobin.isnot(None)).group_by(ANCVisit.pregnant_woman_id).subquery()
    hb = hb_sub.c.hb
    h_none, h_sev, h_mod, h_mild, h_ok = db.query(
        func.sum(case((hb.is_(None), 1), else_=0)),
        func.sum(case((hb < HB_MODERATE, 1), else_=0)),
        func.sum(case((and_(hb >= HB_MODERATE, hb < HB_MILD), 1), else_=0)),
        func.sum(case((and_(hb >= HB_MILD, hb < HB_NORMAL), 1), else_=0)),
        func.sum(case((hb >= HB_NORMAL, 1), else_=0)),
    ).select_from(PW).outerjoin(hb_sub, hb_sub.c.pid == PW.id).filter(*conds, PW.is_active == True).one()  # noqa: E712
    h_none, h_sev, h_mod, h_mild, h_ok = map(_i, (h_none, h_sev, h_mod, h_mild, h_ok))
    with_hb = h_sev + h_mod + h_mild + h_ok
    anaemia = {
        "with_hb_recorded": with_hb, "no_hb_recorded": h_none,
        "anaemic_pct": _pct(h_sev + h_mod + h_mild, with_hb),
        "categories": [
            {"key": "normal", "label": "Normal (11+ g/dL)", "count": h_ok},
            {"key": "mild", "label": "Mild (10–10.9)", "count": h_mild},
            {"key": "moderate", "label": "Moderate (7–9.9)", "count": h_mod},
            {"key": "severe", "label": "Severe (<7)", "count": h_sev},
            {"key": "none", "label": "No Hb recorded", "count": h_none},
        ],
    }

    # ---- ANC visits inside the period
    vis_f = [*conds, ANCVisit.visit_date >= s, ANCVisit.visit_date <= e]
    by_no: Counter = Counter()
    for vno, c in db.query(ANCVisit.visit_number, func.count(ANCVisit.id)).join(PW, PW.id == ANCVisit.pregnant_woman_id) \
            .filter(*vis_f).group_by(ANCVisit.visit_number).all():
        by_no[min(_i(vno), 4)] += _i(c)
    flags = db.query(func.count(ANCVisit.id),
                     func.sum(case((ANCVisit.is_emergency == True, 1), else_=0)),  # noqa: E712
                     func.sum(case((ANCVisit.referred_for_usg == True, 1), else_=0))) \
        .join(PW, PW.id == ANCVisit.pregnant_woman_id).filter(*vis_f).one()
    anc_period = {
        "total_visits": _i(flags[0]), "emergency_visits": _i(flags[1]), "referred_for_usg": _i(flags[2]),
        "by_visit_number": [{"label": ("ANC " + str(k) + ("+" if k == 4 else "")), "count": by_no.get(k, 0)} for k in (1, 2, 3, 4)],
    }

    # ---- USG (appointments scheduled inside the period)
    ubase = [*conds, USGAppointment.scheduled_date >= _start_dt(s), USGAppointment.scheduled_date < _end_dt(e)]

    def usg_q(*cols):
        return db.query(*cols).join(PW, PW.id == USGAppointment.pregnant_woman_id)

    status_counts = {_ev(k): _i(v) for k, v in usg_q(USGAppointment.status, func.count(USGAppointment.id)).filter(*ubase).group_by(USGAppointment.status).all()}
    type_counts = {_ev(k): _i(v) for k, v in usg_q(USGAppointment.appointment_type, func.count(USGAppointment.id)).filter(*ubase).group_by(USGAppointment.appointment_type).all()}
    scan_types = [{"label": k or "Not recorded", "count": _i(v)} for k, v in
                  usg_q(USGAppointment.scan_type, func.count(USGAppointment.id))
                  .filter(*ubase, USGAppointment.status == AppointmentStatus.COMPLETED)
                  .group_by(USGAppointment.scan_type).order_by(func.count(USGAppointment.id).desc()).all()]
    usg_total = sum(status_counts.values())
    completed = status_counts.get("completed", 0)
    abnormal = _i(usg_q(func.count(USGAppointment.id)).filter(*ubase, USGAppointment.status == AppointmentStatus.COMPLETED, USGAppointment.is_high_risk == True).scalar())  # noqa: E712
    rescheduled = _i(usg_q(func.count(USGAppointment.id)).filter(*ubase, USGAppointment.reschedule_count > 0).scalar())
    overdue_by_centre = {cid: _i(c) for cid, c in usg_q(USGAppointment.usg_centre_id, func.count(USGAppointment.id)).filter(
        *conds, USGAppointment.status.in_(OPEN_USG), USGAppointment.scheduled_date < _start_dt(today)).group_by(USGAppointment.usg_centre_id).all()}

    per_centre: Dict[int, dict] = defaultdict(lambda: {"total": 0, "completed": 0, "cancelled": 0, "delays": []})
    for cid, st, c in usg_q(USGAppointment.usg_centre_id, USGAppointment.status, func.count(USGAppointment.id)) \
            .filter(*ubase).group_by(USGAppointment.usg_centre_id, USGAppointment.status).all():
        per_centre[cid]["total"] += _i(c)
        if _ev(st) == "completed":
            per_centre[cid]["completed"] += _i(c)
        elif _ev(st) == "cancelled":
            per_centre[cid]["cancelled"] += _i(c)
    all_delays: List[float] = []
    for cid, sched, done in usg_q(USGAppointment.usg_centre_id, USGAppointment.scheduled_date, USGAppointment.completed_date) \
            .filter(*ubase, USGAppointment.status == AppointmentStatus.COMPLETED, USGAppointment.completed_date.isnot(None)).all():
        d = max((done - sched).total_seconds() / 86400, 0.0)
        per_centre[cid]["delays"].append(d)
        all_delays.append(d)
    for cid in overdue_by_centre:
        per_centre[cid]  # ensure centres that only have overdue scans still appear
    names = _name_map(db, USGCentre, per_centre.keys())
    centre_rows = sorted((
        {"usg_centre_id": cid, "name": names.get(cid, f"Centre {cid}"), "total": v["total"], "completed": v["completed"],
         "cancelled": v["cancelled"], "completion_pct": _pct(v["completed"], v["total"]),
         "overdue_now": overdue_by_centre.get(cid, 0),
         "avg_delay_days": round(sum(v["delays"]) / len(v["delays"]), 1) if v["delays"] else None}
        for cid, v in per_centre.items()), key=lambda r: -r["total"])

    usg = {
        "total": usg_total,
        "status": [{"key": k, "label": k.replace("_", " ").title(), "count": v} for k, v in status_counts.items()],
        "completion_pct": _pct(completed, usg_total),
        "emergency": type_counts.get("emergency", 0), "emergency_pct": _pct(type_counts.get("emergency", 0), usg_total),
        "rescheduled": rescheduled, "rescheduled_pct": _pct(rescheduled, usg_total),
        "flagged_high_risk": abnormal, "flagged_pct": _pct(abnormal, completed),
        "avg_delay_days": round(sum(all_delays) / len(all_delays), 1) if all_delays else None,
        "overdue_now": sum(overdue_by_centre.values()),
        "scan_types": scan_types,
        "centres": centre_rows,
    }

    # ---- PMSMA (sessions scheduled inside the period, per centre)
    pbase = [*conds, PMSMASession.scheduled_date >= _start_dt(s), PMSMASession.scheduled_date < _end_dt(e)]

    def pmsma_q(*cols):
        return db.query(*cols).join(PW, PW.id == PMSMASession.pregnant_woman_id)

    OPEN_PMSMA = ("scheduled", "rescheduled")
    per_pmsma_centre: Dict[int, dict] = defaultdict(lambda: {"scheduled": 0, "attended": 0, "cancelled": 0})
    for cid, st, c in pmsma_q(PMSMASession.pmsma_centre_id, PMSMASession.status, func.count(PMSMASession.id)) \
            .filter(*pbase).group_by(PMSMASession.pmsma_centre_id, PMSMASession.status).all():
        v = per_pmsma_centre[cid]
        v["scheduled"] += _i(c)
        st = _ev(st)
        if st == "completed":
            v["attended"] += _i(c)
        elif st == "cancelled":
            v["cancelled"] += _i(c)

    # "Missed": still open (scheduled/rescheduled) but the date has already passed — a now-snapshot, not period-bound.
    missed_by_pmsma_centre = {cid: _i(c) for cid, c in pmsma_q(PMSMASession.pmsma_centre_id, func.count(PMSMASession.id)).filter(
        *conds, PMSMASession.status.in_(OPEN_PMSMA), PMSMASession.scheduled_date < _start_dt(today)).group_by(PMSMASession.pmsma_centre_id).all()}

    # "Next session": earliest upcoming open session per centre — also a now-snapshot.
    next_by_pmsma_centre = {cid: nxt for cid, nxt in pmsma_q(PMSMASession.pmsma_centre_id, func.min(PMSMASession.scheduled_date)).filter(
        *conds, PMSMASession.status.in_(OPEN_PMSMA), PMSMASession.scheduled_date >= _start_dt(today)).group_by(PMSMASession.pmsma_centre_id).all()}

    for cid in (*missed_by_pmsma_centre, *next_by_pmsma_centre):
        per_pmsma_centre[cid]  # ensure centres that only show up "now" still appear

    pmsma_names = _name_map(db, PMSMACentre, per_pmsma_centre.keys())
    pmsma_centre_rows = sorted((
        {"pmsma_centre_id": cid, "name": pmsma_names.get(cid, f"Centre {cid}"), "scheduled": v["scheduled"],
         "attended": v["attended"], "attendance_pct": _pct(v["attended"], v["scheduled"]),
         "missed": missed_by_pmsma_centre.get(cid, 0), "cancelled": v["cancelled"],
         "next_session": next_by_pmsma_centre.get(cid)}
        for cid, v in per_pmsma_centre.items()), key=lambda r: -r["scheduled"])

    pmsma = {
        "total_scheduled": sum(v["scheduled"] for v in per_pmsma_centre.values()),
        "total_attended": sum(v["attended"] for v in per_pmsma_centre.values()),
        "centres": pmsma_centre_rows,
    }

    return {
        "period": {"start_date": s, "end_date": e},
        "anc": {"funnel": funnel, "avg_visits": round(float(avg_n or 0), 1), "on_track": on_track,
                "anaemia": anaemia, "period": anc_period},
        "usg": usg,
        "pmsma": pmsma,
        "filters": f.describe(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. Delivery & outcomes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/delivery")
async def delivery(
    f: PWFilters = Depends(pw_filters_dep),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    _guard(user)
    s, e, _, _ = resolve_period(f.start_date, f.end_date)
    conds = _cohort(db, f, user)
    R, O = DeliveryReferral, DeliveryOutcome

    rbase = [*conds, R.created_at >= _start_dt(s), R.created_at < _end_dt(e)]
    obase = [*conds, O.delivery_date >= _start_dt(s), O.delivery_date < _end_dt(e)]

    def rq(*cols):
        return db.query(*cols).join(PW, PW.id == R.pregnant_woman_id)

    def oq(*cols):
        return db.query(*cols).join(PW, PW.id == O.pregnant_woman_id)

    # Referral funnel
    rstatus = {k: _i(v) for k, v in rq(R.status, func.count(R.id)).filter(*rbase).group_by(R.status).all()}
    total_ref = sum(rstatus.values())
    accept_hours = [max((acc - cr).total_seconds() / 3600, 0.0) for cr, acc in
                    rq(R.created_at, R.accepted_at).filter(*rbase, R.accepted_at.isnot(None)).all()]

    # Outcomes
    dtypes = [{"key": _ev(k), "label": str(_ev(k)).replace("_", " ").title(), "count": _i(v)} for k, v in
              oq(O.delivery_type, func.count(O.id)).filter(*obase).group_by(O.delivery_type).all()]
    mo = [{"key": _ev(k), "label": str(_ev(k)).replace("_", " ").title(), "count": _i(v)} for k, v in
          oq(O.maternal_outcome, func.count(O.id)).filter(*obase).group_by(O.maternal_outcome).all()]
    total_del = sum(d["count"] for d in dtypes)
    hr_del = _i(oq(func.count(O.id)).filter(*obase, PW.is_high_risk == True).scalar())  # noqa: E712

    bq = db.query(DeliveryOutcomeBaby.status, DeliveryOutcomeBaby.gender, func.count(DeliveryOutcomeBaby.id)) \
        .join(O, O.id == DeliveryOutcomeBaby.outcome_id).join(PW, PW.id == O.pregnant_woman_id) \
        .filter(*obase).group_by(DeliveryOutcomeBaby.status, DeliveryOutcomeBaby.gender).all()
    baby_status: Counter = Counter()
    baby_gender: Counter = Counter()
    for st, gd, c in bq:
        baby_status[st] += _i(c)
        baby_gender[(gd or "unknown").lower()] += _i(c)
    babies_total = sum(baby_status.values())

    # Length of stay (admission → discharge) for discharges in the period
    stays = [max((dis - adm).total_seconds() / 86400, 0.0) for adm, dis in
             db.query(Admission.admission_date, Admission.discharge_date).join(PW, PW.id == Admission.pregnant_woman_id)
             .filter(*conds, Admission.discharge_date.isnot(None),
                     Admission.discharge_date >= _start_dt(s), Admission.discharge_date < _end_dt(e)).all()]

    # Per delivery point
    dp: Dict[int, dict] = defaultdict(lambda: {"referrals": 0, "accepted": 0, "completed": 0, "re_referred": 0, "pending": 0,
                                               "deliveries": 0, "maternal_deaths": 0, "infant_deaths": 0, "still_births": 0})
    for dpid, st, c in rq(R.dp_id, R.status, func.count(R.id)).filter(*rbase).group_by(R.dp_id, R.status).all():
        dp[dpid]["referrals"] += _i(c)
        if st in dp[dpid]:
            dp[dpid][st] += _i(c)
    for dpid, c in oq(O.dp_id, func.count(O.id)).filter(*obase).group_by(O.dp_id).all():
        dp[dpid]["deliveries"] += _i(c)
    for dpid, c in oq(O.dp_id, func.count(O.id)).filter(*obase, or_(O.maternal_outcome == MaternalOutcome.MATERNAL_DEATH, O.delivery_type == DeliveryType.MATERNAL_DEATH)).group_by(O.dp_id).all():
        dp[dpid]["maternal_deaths"] += _i(c)
    for dpid, st, c in db.query(O.dp_id, DeliveryOutcomeBaby.status, func.count(DeliveryOutcomeBaby.id)) \
            .join(O, O.id == DeliveryOutcomeBaby.outcome_id).join(PW, PW.id == O.pregnant_woman_id) \
            .filter(*obase, DeliveryOutcomeBaby.status.in_(["infant_death", "still_birth"])).group_by(O.dp_id, DeliveryOutcomeBaby.status).all():
        dp[dpid]["infant_deaths" if st == "infant_death" else "still_births"] += _i(c)
    names = _name_map(db, DeliveryPoint, dp.keys())
    dp_rows = sorted(({"dp_id": k, "name": names.get(k, f"DP {k}"), **v,
                       "completion_pct": _pct(v["completed"], v["referrals"])} for k, v in dp.items()),
                     key=lambda r: -r["referrals"])

    return {
        "period": {"start_date": s, "end_date": e},
        "referrals": {
            "total": total_ref,
            "status": [{"key": k, "label": k.replace("_", " ").title(), "count": rstatus.get(k, 0)}
                       for k in ("pending", "accepted", "re_referred", "completed")],
            "re_referral_pct": _pct(rstatus.get("re_referred", 0), total_ref),
            "avg_hours_to_accept": round(sum(accept_hours) / len(accept_hours), 1) if accept_hours else None,
        },
        "outcomes": {
            "total": total_del, "high_risk_deliveries": hr_del, "high_risk_pct": _pct(hr_del, total_del),
            "delivery_types": dtypes, "maternal_outcomes": mo,
            "babies_total": babies_total,
            "baby_status": [{"key": k, "label": k.replace("_", " ").title(), "count": v} for k, v in baby_status.items()],
            "baby_gender": [{"label": k.title(), "count": v} for k, v in baby_gender.items()],
            "avg_stay_days": round(sum(stays) / len(stays), 1) if stays else None,
            "discharges": len(stays),
        },
        "delivery_points": dp_rows,
        "filters": f.describe(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7. Geography – compare blocks (or sub-centres / wards once you drill down)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/geography")
async def geography(
    f: PWFilters = Depends(pw_filters_dep),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """
    One row per unit at the level below the current filter:
        no geography filter  -> blocks
        block selected       -> sub-centres in that block
        sub-centre / ward    -> wards
    """
    _guard(user)
    s, e, _, _ = resolve_period(f.start_date, f.end_date)
    conds = _cohort(db, f, user)

    if f.sub_centre_id or f.ward_id:
        level, key, model = "ward", PW.ward_id, Ward
    elif f.block_id:
        level, key, model = "sub_centre", PW.sub_centre_id, SubCentre
    else:
        level, key, model = "block", PW.block_id, Block

    rows: Dict[Optional[int], dict] = defaultdict(lambda: {
        "women": 0, "active": 0, "high_risk": 0, "registrations": 0, "anc1": 0, "anc4": 0,
        "no_anc": 0, "anc_visits": 0, "usg_total": 0, "usg_completed": 0, "usg_overdue": 0,
        "deliveries": 0})

    vc = _visit_counts_subquery(db, e)
    n = func.coalesce(vc.c.n, 0)
    act = PW.is_active == True  # noqa: E712
    reg_in = and_(PW.created_at >= _start_dt(s), PW.created_at < _end_dt(e))
    q = db.query(
        key,
        func.count(PW.id),
        func.sum(case((act, 1), else_=0)),
        func.sum(case((PW.is_high_risk == True, 1), else_=0)),  # noqa: E712
        func.sum(case((reg_in, 1), else_=0)),
        func.sum(case((and_(act, n >= 1), 1), else_=0)),
        func.sum(case((and_(act, n >= 4), 1), else_=0)),
        func.sum(case((and_(act, n == 0), 1), else_=0)),
    ).select_from(PW).outerjoin(vc, vc.c.pid == PW.id).filter(*conds).group_by(key)
    for k, w, a, hr, rg, a1, a4, na in q.all():
        r = rows[k]
        r.update(women=_i(w), active=_i(a), high_risk=_i(hr), registrations=_i(rg), anc1=_i(a1), anc4=_i(a4), no_anc=_i(na))

    for k, c in db.query(key, func.count(ANCVisit.id)).select_from(ANCVisit).join(PW, PW.id == ANCVisit.pregnant_woman_id) \
            .filter(*conds, ANCVisit.visit_date >= s, ANCVisit.visit_date <= e).group_by(key).all():
        rows[k]["anc_visits"] = _i(c)

    today0 = _start_dt(date.today())
    for k, tot, done, od in db.query(
            key, func.count(USGAppointment.id),
            func.sum(case((USGAppointment.status == AppointmentStatus.COMPLETED, 1), else_=0)),
            func.sum(case((and_(USGAppointment.status.in_(OPEN_USG), USGAppointment.scheduled_date < today0), 1), else_=0))) \
            .select_from(USGAppointment).join(PW, PW.id == USGAppointment.pregnant_woman_id) \
            .filter(*conds, USGAppointment.scheduled_date >= _start_dt(s), USGAppointment.scheduled_date < _end_dt(e)).group_by(key).all():
        rows[k].update(usg_total=_i(tot), usg_completed=_i(done), usg_overdue=_i(od))

    for k, c in db.query(key, func.count(DeliveryOutcome.id)).select_from(DeliveryOutcome) \
            .join(PW, PW.id == DeliveryOutcome.pregnant_woman_id) \
            .filter(*conds, DeliveryOutcome.delivery_date >= _start_dt(s), DeliveryOutcome.delivery_date < _end_dt(e)).group_by(key).all():
        rows[k]["deliveries"] = _i(c)

    # Make sure every unit at this level appears, even with no women yet
    if level == "block":
        universe = db.query(Block.id, Block.name).filter(Block.district_id == user.district_id, Block.is_active == True).all()  # noqa: E712
    elif level == "sub_centre":
        universe = db.query(SubCentre.id, SubCentre.name).filter(SubCentre.block_id == f.block_id, SubCentre.is_active == True).all()  # noqa: E712
    else:
        wq = db.query(Ward.id, Ward.name).filter(Ward.is_active == True)  # noqa: E712
        if f.ward_id:
            wq = wq.filter(Ward.id == f.ward_id)
        elif f.sub_centre_id:
            from models import WardSubcentreMapping
            wq = wq.filter(Ward.id.in_(select(WardSubcentreMapping.ward_id).where(WardSubcentreMapping.sub_centre_id == f.sub_centre_id)))
        universe = wq.all()
    names = {i: nm for i, nm in universe}
    # Drop any units outside the current drill-down (e.g. other wards) that have no women.
    for k in [k for k, r in rows.items() if k not in names and not r["women"]]:
        del rows[k]
    for i in names:
        rows[i]  # make sure units with no women yet still appear
    missing = [k for k in rows if k is not None and k not in names]
    names.update(_name_map(db, model, missing))

    out = []
    for k, r in rows.items():
        if k is None and not r["women"]:
            continue
        out.append({
            "id": k, "name": names.get(k, "Unassigned") if k is not None else "Unassigned", **r,
            "hr_pct": _pct(r["high_risk"], r["women"]),
            "anc1_pct": _pct(r["anc1"], r["active"]),
            "anc4_pct": _pct(r["anc4"], r["active"]),
            "usg_completion_pct": _pct(r["usg_completed"], r["usg_total"]),
        })
    out.sort(key=lambda r: r["name"] or "")

    def total(field):
        return sum(r[field] for r in out)

    dist = {k: total(k) for k in ("women", "active", "high_risk", "registrations", "anc1", "anc4", "no_anc", "anc_visits",
                                   "usg_total", "usg_completed", "usg_overdue", "deliveries")}
    dist.update(hr_pct=_pct(dist["high_risk"], dist["women"]), anc1_pct=_pct(dist["anc1"], dist["active"]),
                anc4_pct=_pct(dist["anc4"], dist["active"]), usg_completion_pct=_pct(dist["usg_completed"], dist["usg_total"]))
    return {"level": level, "period": {"start_date": s, "end_date": e}, "rows": out, "overall": dist, "filters": f.describe()}


# ─────────────────────────────────────────────────────────────────────────────
# 8. Watch-list – the people/cases that need action today
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/watchlist")
async def watchlist(
    limit: int = Query(10, ge=1, le=100),
    anc_gap_days: int = Query(30, ge=7, le=180, description="High-risk women unseen for this many days"),
    due_within_days: int = Query(30, ge=7, le=90),
    f: PWFilters = Depends(pw_filters_dep),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    _guard(user)
    conds = _cohort(db, f, user)
    today = date.today()
    now = datetime.now()

    def lookups(pws):
        return (_name_map(db, Block, [p.block_id for p in pws]), _name_map(db, SubCentre, [p.sub_centre_id for p in pws]))

    # (a) high-risk, ongoing, no ANC contact for N days
    last = db.query(ANCVisit.pregnant_woman_id.label("pid"), func.max(ANCVisit.visit_date).label("last")) \
        .group_by(ANCVisit.pregnant_woman_id).subquery()
    a_f = [*conds, PW.is_active == True, PW.is_high_risk == True,  # noqa: E712
           or_(last.c.last.is_(None), last.c.last < today - timedelta(days=anc_gap_days))]
    a_total = db.query(func.count(PW.id)).select_from(PW).outerjoin(last, last.c.pid == PW.id).filter(*a_f).scalar() or 0
    a_rows = db.query(PW, last.c.last).outerjoin(last, last.c.pid == PW.id).filter(*a_f) \
        .order_by(last.c.last.isnot(None), last.c.last.asc()).limit(limit).all()
    bm, sm = lookups([p for p, _ in a_rows])
    a_act = _action_status(db, [p.id for p, _ in a_rows], ("missed_anc", "hrp_unreferred"))
    hr_gap = [_with_action({**_person(p, bm, sm), "last_anc": ls, "days_since": (today - ls).days if ls else None}, a_act) for p, ls in a_rows]

    # (b) overdue USG
    U = USGAppointment
    b_f = [*conds, U.status.in_(OPEN_USG), U.scheduled_date < _start_dt(today)]
    b_total = db.query(func.count(U.id)).join(PW, PW.id == U.pregnant_woman_id).filter(*b_f).scalar() or 0
    b_rows = db.query(PW, U).join(U, U.pregnant_woman_id == PW.id).filter(*b_f).order_by(U.scheduled_date.asc()).limit(limit).all()
    bm, sm = lookups([p for p, _ in b_rows])
    cn = _name_map(db, USGCentre, [u.usg_centre_id for _, u in b_rows])
    b_act = _action_status(db, [p.id for p, _ in b_rows], ("missed_usg",))
    overdue = [_with_action({**_person(p, bm, sm), "scheduled_date": u.scheduled_date, "days_overdue": (today - u.scheduled_date.date()).days,
                "status": _ev(u.status), "appointment_type": _ev(u.appointment_type), "usg_centre": cn.get(u.usg_centre_id)}, b_act) for p, u in b_rows]

    # (c) EDD close (or passed) with no delivery referral
    has_ref = exists().where(DeliveryReferral.pregnant_woman_id == PW.id).correlate(PW)
    c_f = [*conds, PW.is_active == True, PW.edd_date.isnot(None), PW.edd_date <= today + timedelta(days=due_within_days), ~has_ref]  # noqa: E712
    c_total = db.query(func.count(PW.id)).filter(*c_f).scalar() or 0
    c_rows = db.query(PW).filter(*c_f).order_by(PW.edd_date.asc()).limit(limit).all()
    bm, sm = lookups(c_rows)
    c_act = _action_status(db, [p.id for p in c_rows], ("near_edd", "hrp_unreferred"))
    due = [_with_action({**_person(p, bm, sm), "days_to_edd": (p.edd_date - today).days}, c_act) for p in c_rows]

    # (d) referrals still pending after 24h
    R = DeliveryReferral
    d_f = [*conds, R.status == "pending", R.created_at < now - timedelta(hours=24)]
    d_total = db.query(func.count(R.id)).join(PW, PW.id == R.pregnant_woman_id).filter(*d_f).scalar() or 0
    d_rows = db.query(PW, R).join(R, R.pregnant_woman_id == PW.id).filter(*d_f).order_by(R.created_at.asc()).limit(limit).all()
    bm, sm = lookups([p for p, _ in d_rows])
    dpn = _name_map(db, DeliveryPoint, [r.dp_id for _, r in d_rows])
    pend = [{**_person(p, bm, sm), "referred_at": r.created_at, "hours_pending": int((now - r.created_at).total_seconds() // 3600),
             "delivery_point": dpn.get(r.dp_id),
             # a pending referral has, by definition, not been accepted by the delivery point yet
             "action_status": {"key": "awaiting_dp", "detail": dpn.get(r.dp_id)}} for p, r in d_rows]

    return {
        "as_of": today,
        "lists": [
            {"key": "hr_no_anc", "title": f"High-risk women with no ANC in {anc_gap_days}+ days", "total": a_total, "items": hr_gap,
             "link": "/pregnant-women-management?tab=high_risk"},
            {"key": "overdue_usg", "title": "Overdue USG appointments", "total": b_total, "items": overdue,
             "link": "/usg-appointment-management?tab=all"},
            {"key": "due_no_referral", "title": f"EDD within {due_within_days} days, no delivery referral", "total": c_total, "items": due,
             "link": "/pregnant-women-management?tab=all"},
            {"key": "pending_referrals", "title": "Referrals pending for over 24 hours", "total": d_total, "items": pend,
             "link": "/delivery-referral-management?tab=pending"},
        ],
        "filters": f.describe(),
    }