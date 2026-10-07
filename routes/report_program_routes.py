"""
Programme reports: PMSMA sessions, Mobilisation worklist, PNC follow-up.

These sit next to the beneficiary reports in report_routes.py and follow the same rules:

* every beneficiary filter (block / sub-centre / ward, risk, age band, trimester, parity,
  blood group, registration source, approval, risk factors, anaemia, ANC contact) is the
  SAME filter object the other reports use (analytics_filters.PWFilters), so a filter means
  exactly the same thing everywhere;
* jurisdiction is enforced in SQL and fails closed for any role that is not listed;
* dates are inclusive calendar days (a DATETIME column is compared with `>= day 00:00` and
  `< next day 00:00`, never with BETWEEN(date, date));
* filtering, searching, sorting and paging all happen in SQL, summary cards describe the
  WHOLE filtered result (not just the visible page) and Excel / CSV exports contain exactly
  the rows the filters select.

Nothing here is cached: these are operational worklist reports that people open straight
after acting on a case.
"""
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, extract, false, func, or_
from sqlalchemy.orm import Session

from analytics_filters import (
    HB_MILD, HB_MODERATE, HB_NORMAL, PWFilters, gestational_weeks, make_pw_filters_dep,
    pw_conditions, scope_conditions, trimester_of,
)
from auth import get_current_active_user
from database import get_db
from models import (
    Block, DeliveryPoint, Discharge, MobilisationCase, PMSMACentre, PMSMASession, PNCReminder,
    PregnantWoman, SubCentre, User, Ward,
)
from routes.report_routes import (
    MAX_EXPORT_ROWS, _dt_between, _enum_key, _pager, _pct2, _person_place, _place_lookup,
    _search_cond, generate_csv_report, generate_excel_report, generate_visualization_config,
)

router = APIRouter(prefix="/reports", tags=["Programme Reports"])

PW = PregnantWoman
PMSMA_OPEN = ("scheduled", "rescheduled")
PMSMA_STATUSES = ("scheduled", "rescheduled", "completed", "cancelled", "missed")
MOB_OPEN = ("pending", "escalated")
MOB_STATUSES = ("pending", "escalated", "mobilised", "closed", "open")
MOB_TRIGGERS = {
    "missed_anc": "Missed ANC",
    "missed_pmsma": "Missed PMSMA",
    "missed_usg": "Missed USG",
    "near_edd": "Near EDD",
    "hrp_unreferred": "High-risk, not referred",
}
MOB_LEVELS = ("anm", "block", "district")
PNC_STATUSES = ("upcoming", "due_today", "overdue", "completed")

pmsma_filters_dep = make_pw_filters_dep("all")
mob_filters_dep = make_pw_filters_dep("all")
pnc_filters_dep = make_pw_filters_dep("all")


# ── small helpers ────────────────────────────────────────────────────────────

def _day_start(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


def _choice(value: Optional[str], allowed, name: str) -> Optional[str]:
    value = (value or "").strip().lower() or None
    if value is not None and value not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid {name} '{value}'. Allowed: {', '.join(allowed)}")
    return value


def _period(f: PWFilters, default_days: int, today: date):
    """Inclusive (start, end).  Future ends are allowed - upcoming sessions / visits are legitimate to report on."""
    end = f.end_date or today
    start = f.start_date or (end - timedelta(days=default_days - 1))
    return start, end


def _names(db: Session, model, ids) -> Dict[int, str]:
    ids = [i for i in set(ids) if i is not None]
    if not ids:
        return {}
    return {r.id: r.name for r in db.query(model.id, model.name).filter(model.id.in_(ids)).all()}


def _user_names(db: Session, ids) -> Dict[int, str]:
    ids = [i for i in set(ids) if i is not None]
    if not ids:
        return {}
    return {r.id: r.full_name for r in db.query(User.id, User.full_name).filter(User.id.in_(ids)).all()}


def _iso(v):
    """Excel/CSV friendly: strip time zone-less datetimes down to plain values."""
    return v.replace(microsecond=0) if isinstance(v, datetime) else v


def _sum_case(cond):
    return func.sum(case((cond, 1), else_=0))


def _i(v) -> int:
    return int(v or 0)


def _bp_high(bp: Optional[str]) -> Optional[bool]:
    """True when a 'sys/dia' string is >= 140/90; None when it cannot be read."""
    try:
        sys_s, dia_s = str(bp).replace(" ", "").split("/")[:2]
        return float(sys_s) >= 140 or float(dia_s) >= 90
    except (ValueError, TypeError):
        return None


def _hb_band(hb: float) -> str:
    if hb >= HB_NORMAL:
        return "normal"
    if hb >= HB_MILD:
        return "mild"
    if hb >= HB_MODERATE:
        return "moderate"
    return "severe"


def _export(format: str, sheets: Dict[str, list], filename: str):
    if format == "excel":
        return generate_excel_report(sheets, f"{filename}.xlsx")
    return generate_csv_report(next(iter(sheets.values())), f"{filename}.csv")


# ═════════════════════════════════════════════════════════════════════════════
# 1. PMSMA SESSIONS REPORT
# ═════════════════════════════════════════════════════════════════════════════

PMSMA_SORTS = ("scheduled", "name", "status", "hb", "blood_sugar")
S = PMSMASession


def _pmsma_scope(user: User):
    """(conditions on PregnantWoman, conditions on PMSMASession) - fails closed."""
    role = user.role
    if role == "pmsma":
        if user.block_id is None:
            return [false()], []
        sess = [S.block_id == user.block_id]
        if user.pmsma_centre_id:
            # same rule as the PMSMA queue: own centre + sessions booked against a free-text site
            sess.append(or_(S.pmsma_centre_id == user.pmsma_centre_id, S.pmsma_centre_id.is_(None)))
        return [], sess
    if role in ("district", "block", "sub_centre"):
        return scope_conditions(user), []
    return [false()], []


def _pmsma_params(status, appointment_type, pmsma_centre_id, high_risk_only, emergency_override, low_hb, search, sort_by, sort_dir):
    return {
        "status": _choice(status, PMSMA_STATUSES, "status"),
        "appointment_type": _choice(appointment_type, ("regular", "emergency"), "appointment_type"),
        "pmsma_centre_id": pmsma_centre_id,
        "high_risk_only": bool(high_risk_only),
        "emergency_override": bool(emergency_override),
        "low_hb": bool(low_hb),
        "search": (search or "").strip() or None,
        "sort_by": sort_by if sort_by in PMSMA_SORTS else "scheduled",
        "sort_dir": "asc" if sort_dir == "asc" else "desc",
    }


def _run_pmsma_report(db: Session, user: User, f: PWFilters, p: dict, page: int, page_size: Optional[int]):
    today = date.today()
    start, end = _period(f, 30, today)
    today_start = _day_start(today)
    past_open = and_(S.status.in_(PMSMA_OPEN), S.scheduled_date < today_start)   # open and the day has passed == missed

    pw_scope, sess_scope = _pmsma_scope(user)
    # Everything except the status selector: drives the summary cards, the charts and the status split,
    # so they always describe the same population the filters describe.
    base = pw_conditions(f, None, include_scope=False) + pw_scope + sess_scope + [_dt_between(S.scheduled_date, start, end)]
    if p["appointment_type"]:
        base.append(S.appointment_type == p["appointment_type"])
    if p["pmsma_centre_id"]:
        base.append(S.pmsma_centre_id == p["pmsma_centre_id"])
    if p["high_risk_only"]:
        base.append(or_(S.is_high_risk == True, PW.is_high_risk == True))  # noqa: E712
    if p["emergency_override"]:
        base.append(S.is_emergency_override == True)  # noqa: E712
    if p["low_hb"]:
        base += [S.status == "completed", S.hb.isnot(None), S.hb < HB_NORMAL]
    sc = _search_cond(p["search"], [PW.full_name, PW.mobile_number, PW.rch_id, PW.abha_id, S.site])
    if sc is not None:
        base.append(sc)

    conds = list(base)
    st = p["status"]
    if st == "missed":
        conds.append(past_open)
    elif st in ("scheduled", "rescheduled"):
        conds += [S.status == st, S.scheduled_date >= today_start]     # still to come
    elif st in ("completed", "cancelled"):
        conds.append(S.status == st)

    def on(q, cs):
        return q.select_from(S).join(PW, PW.id == S.pregnant_woman_id).filter(*cs)

    total = _i(on(db.query(func.count(S.id)), conds).scalar())

    # ── Summary (whole filtered population) ──
    raw = {(_enum_key(k)): _i(v) for k, v in on(db.query(S.status, func.count(S.id)), base).group_by(S.status).all()}
    past = {(_enum_key(k)): _i(v) for k, v in on(db.query(S.status, func.count(S.id)), base + [past_open]).group_by(S.status).all()}
    missed = sum(past.values())
    status_breakdown = {
        "completed": raw.get("completed", 0),
        "scheduled": raw.get("scheduled", 0) - past.get("scheduled", 0),
        "rescheduled": raw.get("rescheduled", 0) - past.get("rescheduled", 0),
        "missed": missed,
        "cancelled": raw.get("cancelled", 0),
    }
    scoped_total = sum(raw.values())
    uniq, n_emerg, n_hr, n_override, n_resched = on(db.query(
        func.count(func.distinct(S.pregnant_woman_id)),
        _sum_case(S.appointment_type == "emergency"),
        _sum_case(or_(S.is_high_risk == True, PW.is_high_risk == True)),  # noqa: E712
        _sum_case(S.is_emergency_override == True),  # noqa: E712
        _sum_case(S.original_scheduled_date.isnot(None)),
    ), base).one()
    due = status_breakdown["completed"] + missed

    # Clinical picture of the completed sessions (Hb, BP, sugar, weight)
    hbs, sugars, weights, high_bp, bp_read = [], [], [], 0, 0
    for bp, hb, sugar, wt in on(db.query(S.bp, S.hb, S.blood_sugar, S.weight), base + [S.status == "completed"]).all():
        if hb is not None:
            hbs.append(float(hb))
        if sugar is not None:
            sugars.append(float(sugar))
        if wt is not None:
            weights.append(float(wt))
        hi = _bp_high(bp)
        if hi is not None:
            bp_read += 1
            high_bp += 1 if hi else 0
    hb_bands = {"normal": 0, "mild": 0, "moderate": 0, "severe": 0}
    for hb in hbs:
        hb_bands[_hb_band(hb)] += 1
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    clinical = {
        "completed_with_hb": len(hbs), "avg_hb": avg(hbs), "anaemic": len(hbs) - hb_bands["normal"],
        "anaemia_bands": hb_bands, "bp_recorded": bp_read, "high_bp": high_bp,
        "avg_blood_sugar": avg(sugars), "avg_weight": avg(weights),
    }

    # ── Breakdowns ──
    c_rows = on(db.query(S.pmsma_centre_id, func.count(S.id), _sum_case(S.status == "completed"), _sum_case(past_open)), conds).group_by(S.pmsma_centre_id).all()
    c_names = _names(db, PMSMACentre, [r[0] for r in c_rows])
    by_centre = sorted(
        [{"id": r[0], "name": c_names.get(r[0], "Other / free-text site") if r[0] else "Other / free-text site",
          "total": _i(r[1]), "completed": _i(r[2]), "missed": _i(r[3])} for r in c_rows],
        key=lambda x: -x["total"])

    by_block, by_sub = [], []
    if user.role == "district" and not f.block_id:
        rows = on(db.query(PW.block_id, func.count(S.id), _sum_case(S.status == "completed"), _sum_case(past_open)), conds).group_by(PW.block_id).all()
        nm = _names(db, Block, [r[0] for r in rows])
        by_block = sorted([{"id": r[0], "name": nm.get(r[0], "Unassigned"), "total": _i(r[1]), "completed": _i(r[2]), "missed": _i(r[3])} for r in rows], key=lambda x: -x["total"])
    if (user.role in ("block", "pmsma") or f.block_id) and not f.sub_centre_id:
        rows = on(db.query(PW.sub_centre_id, func.count(S.id), _sum_case(S.status == "completed"), _sum_case(past_open)), conds).group_by(PW.sub_centre_id).all()
        nm = _names(db, SubCentre, [r[0] for r in rows])
        by_sub = sorted([{"id": r[0], "name": nm.get(r[0], "Unassigned"), "total": _i(r[1]), "completed": _i(r[2]), "missed": _i(r[3])} for r in rows], key=lambda x: -x["total"])

    yr, mo = extract("year", S.scheduled_date), extract("month", S.scheduled_date)
    m_rows = on(db.query(yr, mo, func.count(S.id), _sum_case(S.status == "completed")), conds).group_by(yr, mo).order_by(yr, mo).all()
    by_month = [{"month": f"{int(r[0]):04d}-{int(r[1]):02d}", "total": _i(r[2]), "completed": _i(r[3])} for r in m_rows]

    # ── Rows ──
    sort_map = {"scheduled": S.scheduled_date, "name": PW.full_name, "status": S.status, "hb": S.hb, "blood_sugar": S.blood_sugar}
    col = sort_map[p["sort_by"]]
    order = [col.desc() if p["sort_dir"] == "desc" else col.asc(), S.id.asc()]
    q = on(db.query(S, PW), conds).order_by(*order)
    rows = q.limit(MAX_EXPORT_ROWS).all() if page_size is None else q.offset((page - 1) * page_size).limit(page_size).all()

    blocks, subs, wards = _place_lookup(db, [r[1] for r in rows])
    centres = _names(db, PMSMACentre, [r[0].pmsma_centre_id for r in rows])
    users = _user_names(db, [r[0].completed_by for r in rows])
    sessions = []
    for s, pw in rows:
        status = (s.status or "").lower()
        is_missed = status in PMSMA_OPEN and s.scheduled_date < today_start
        weeks = gestational_weeks(pw.lmp_date, pw.edd_date, today)
        sessions.append({
            "id": s.id, "pregnant_woman_id": pw.id, "full_name": pw.full_name, "mobile_number": pw.mobile_number, "rch_id": pw.rch_id,
            "scheduled_date": s.scheduled_date, "original_scheduled_date": s.original_scheduled_date,
            "status": "missed" if is_missed else status, "raw_status": status,
            "days_overdue": (today - s.scheduled_date.date()).days if is_missed else 0,
            "appointment_type": s.appointment_type,
            "centre_name": centres.get(s.pmsma_centre_id) or s.site, "site": s.site,
            "bp": s.bp, "hb": s.hb, "blood_sugar": s.blood_sugar, "weight": s.weight,
            "is_high_risk": bool(s.is_high_risk) or bool(pw.is_high_risk),
            "is_emergency_override": bool(s.is_emergency_override), "reschedule_reason": s.reschedule_reason,
            "counselling_notes": s.counselling_notes, "completed_by_name": users.get(s.completed_by),
            "gestational_weeks": weeks, "trimester": trimester_of(weeks) if pw.is_active else None,
            "age": pw.age, "block_name": blocks.get(pw.block_id), "sub_centre_name": subs.get(pw.sub_centre_id), "ward_name": wards.get(pw.ward_id),
        })

    return {
        "report_period": {"start_date": start, "end_date": end, "basis": "scheduled date"},
        "filters": {**f.describe(), **{k: v for k, v in p.items() if v not in (None, "", False) and k not in ("sort_by", "sort_dir")}},
        "summary": {
            "total_sessions": scoped_total,               # every status, all filters except the status selector
            "matching_filters": total,                    # rows in the table (status selector applied as well)
            "unique_women": _i(uniq),
            "completed": status_breakdown["completed"], "upcoming": status_breakdown["scheduled"] + status_breakdown["rescheduled"],
            "missed": missed, "cancelled": status_breakdown["cancelled"],
            "emergency": _i(n_emerg), "high_risk": _i(n_hr), "emergency_override": _i(n_override), "ever_rescheduled": _i(n_resched),
            "due_sessions": due,                          # sessions whose day has come: completed + missed
            "completion_rate": _pct2(status_breakdown["completed"], due),
        },
        "status_breakdown": status_breakdown,
        "clinical": clinical,
        "breakdowns": {"by_centre": by_centre, "by_block": by_block, "by_sub_centre": by_sub, "by_month": by_month},
        "sessions": sessions,
        "pagination": _pager(total, page, page_size or total or 1),
        "visualization": generate_visualization_config(
            [{"status": k.title(), "count": v} for k, v in status_breakdown.items()], "bar", "status", "count", "PMSMA Sessions by Status"),
    }


@router.get("/pmsma")
async def get_pmsma_report(
    f: PWFilters = Depends(pmsma_filters_dep),
    status: Optional[str] = Query(None, description="scheduled | rescheduled | completed | cancelled | missed"),
    appointment_type: Optional[str] = Query(None, description="regular | emergency"),
    pmsma_centre_id: Optional[int] = None,
    high_risk_only: bool = False,
    emergency_override: bool = False,
    low_hb: bool = Query(False, description="completed sessions with Hb below 11 g/dL"),
    search: Optional[str] = None,
    sort_by: str = "scheduled",
    sort_dir: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """PMSMA session report: coverage, completion, missed sessions, clinical findings, by centre / block / sub-centre / month."""
    p = _pmsma_params(status, appointment_type, pmsma_centre_id, high_risk_only, emergency_override, low_hb, search, sort_by, sort_dir)
    return _run_pmsma_report(db, current_user, f, p, page, page_size)


@router.get("/pmsma/export/{format}")
async def export_pmsma_report(
    format: str,
    f: PWFilters = Depends(pmsma_filters_dep),
    status: Optional[str] = None,
    appointment_type: Optional[str] = None,
    pmsma_centre_id: Optional[int] = None,
    high_risk_only: bool = False,
    emergency_override: bool = False,
    low_hb: bool = False,
    search: Optional[str] = None,
    sort_by: str = "scheduled",
    sort_dir: str = "desc",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if format not in ("excel", "csv"):
        raise HTTPException(status_code=400, detail="Format must be 'excel' or 'csv'")
    p = _pmsma_params(status, appointment_type, pmsma_centre_id, high_risk_only, emergency_override, low_hb, search, sort_by, sort_dir)
    data = _run_pmsma_report(db, current_user, f, p, 1, None)
    cols = ["full_name", "mobile_number", "rch_id", "age", "gestational_weeks", "trimester", "scheduled_date", "original_scheduled_date",
            "status", "days_overdue", "appointment_type", "centre_name", "bp", "hb", "blood_sugar", "weight", "is_high_risk",
            "is_emergency_override", "reschedule_reason", "counselling_notes", "completed_by_name", "block_name", "sub_centre_name", "ward_name"]
    rows = [{c: _iso(r[c]) for c in cols} for r in data["sessions"]]
    summary = [{"metric": k.replace("_", " ").title(), "value": v} for k, v in data["summary"].items()]
    summary += [{"metric": f"Clinical - {k.replace('_', ' ').title()}", "value": v} for k, v in data["clinical"].items() if not isinstance(v, dict)]
    return await _export(format, {"PMSMA Sessions": rows, "Summary": summary}, f"pmsma_sessions_{date.today().isoformat()}")


# ═════════════════════════════════════════════════════════════════════════════
# 2. MOBILISATION REPORT
# ═════════════════════════════════════════════════════════════════════════════

MOB_SORTS = ("raised", "name", "status", "age", "trigger", "level")
M = MobilisationCase


def _mob_scope(user: User):
    """Same rule as the Mobilisation worklist itself (the case carries its own block / sub-centre / district)."""
    role = user.role
    if role == "district" and user.district_id is not None:
        return [M.district_id == user.district_id]
    if role == "block" and user.block_id is not None:
        return [M.block_id == user.block_id]
    if role == "sub_centre" and user.sub_centre_id is not None:
        return [M.sub_centre_id == user.sub_centre_id]
    return [false()]


def _mob_params(status, trigger_type, escalation_level, date_basis, older_than_days, search, sort_by, sort_dir):
    return {
        "status": _choice(status, MOB_STATUSES, "status"),
        "trigger_type": _choice(trigger_type, tuple(MOB_TRIGGERS), "trigger_type"),
        "escalation_level": _choice(escalation_level, MOB_LEVELS, "escalation_level"),
        "date_basis": date_basis if date_basis in ("raised", "mobilised") else "raised",
        "older_than_days": older_than_days,
        "search": (search or "").strip() or None,
        "sort_by": sort_by if sort_by in MOB_SORTS else "raised",
        "sort_dir": "asc" if sort_dir == "asc" else "desc",
    }


def _run_mobilisation_report(db: Session, user: User, f: PWFilters, p: dict, page: int, page_size: Optional[int]):
    today = date.today()
    start, end = _period(f, 30, today)
    date_col = M.mobilised_at if p["date_basis"] == "mobilised" else M.created_at

    base = pw_conditions(f, None, include_scope=False) + _mob_scope(user) + [_dt_between(date_col, start, end)]
    if p["trigger_type"]:
        base.append(M.trigger_type == p["trigger_type"])
    if p["escalation_level"]:
        base.append(M.escalation_level == p["escalation_level"])
    if p["older_than_days"]:
        base += [M.status.in_(MOB_OPEN), M.created_at < _day_start(today - timedelta(days=p["older_than_days"] - 1))]
    sc = _search_cond(p["search"], [PW.full_name, PW.mobile_number, PW.rch_id, M.trigger_detail])
    if sc is not None:
        base.append(sc)

    conds = list(base)
    if p["status"] == "open":
        conds.append(M.status.in_(MOB_OPEN))
    elif p["status"]:
        conds.append(M.status == p["status"])

    def on(q, cs):
        return q.select_from(M).join(PW, PW.id == M.pregnant_woman_id).filter(*cs)

    total = _i(on(db.query(func.count(M.id)), conds).scalar())

    # ── Summary ──
    by_status = {_enum_key(k): _i(v) for k, v in on(db.query(M.status, func.count(M.id)), base).group_by(M.status).all()}
    scoped_total = sum(by_status.values())
    open_n = by_status.get("pending", 0) + by_status.get("escalated", 0)
    mobilised_n, closed_n = by_status.get("mobilised", 0), by_status.get("closed", 0)
    uniq = _i(on(db.query(func.count(func.distinct(M.pregnant_woman_id))), base).scalar())
    n_hr = _i(on(db.query(func.count(func.distinct(M.pregnant_woman_id))), base + [PW.is_high_risk == True]).scalar())  # noqa: E712
    n_ever_escalated = _i(on(db.query(func.count(M.id)), base + [M.escalated_at.isnot(None)]).scalar())

    hours = sorted(
        max((m_at - c_at).total_seconds(), 0) / 3600.0
        for c_at, m_at in on(db.query(M.created_at, M.mobilised_at), base + [M.status == "mobilised", M.mobilised_at.isnot(None)]).all()
        if c_at and m_at)
    median_h = (hours[len(hours) // 2] if len(hours) % 2 else (hours[len(hours) // 2 - 1] + hours[len(hours) // 2]) / 2) if hours else None
    ageing = {"0-2 days": 0, "3-7 days": 0, "8-14 days": 0, "15+ days": 0}
    for (c_at,) in on(db.query(M.created_at), base + [M.status.in_(MOB_OPEN)]).all():
        d = (today - c_at.date()).days if c_at else 0
        ageing["0-2 days" if d <= 2 else "3-7 days" if d <= 7 else "8-14 days" if d <= 14 else "15+ days"] += 1

    # ── Breakdowns ──
    def split(rows, name_of):
        return sorted([{"key": r[0], "name": name_of(r[0]), "total": _i(r[1]), "open": _i(r[2]), "mobilised": _i(r[3]), "closed": _i(r[4]),
                        "resolved_rate": _pct2(_i(r[3]) + _i(r[4]), _i(r[1]))} for r in rows], key=lambda x: -x["total"])

    aggs = (func.count(M.id), _sum_case(M.status.in_(MOB_OPEN)), _sum_case(M.status == "mobilised"), _sum_case(M.status == "closed"))
    by_trigger = split(on(db.query(M.trigger_type, *aggs), conds).group_by(M.trigger_type).all(), lambda k: MOB_TRIGGERS.get(k, k))
    by_level = split(on(db.query(M.escalation_level, *aggs), conds).group_by(M.escalation_level).all(), lambda k: (k or "").title())
    by_block, by_sub = [], []
    if user.role == "district" and not f.block_id:
        rows = on(db.query(M.block_id, *aggs), conds).group_by(M.block_id).all()
        nm = _names(db, Block, [r[0] for r in rows])
        by_block = split(rows, lambda k: nm.get(k, "Unassigned"))
    if (user.role in ("district", "block")) and (user.role == "block" or f.block_id) and not f.sub_centre_id:
        rows = on(db.query(M.sub_centre_id, *aggs), conds).group_by(M.sub_centre_id).all()
        nm = _names(db, SubCentre, [r[0] for r in rows])
        by_sub = split(rows, lambda k: nm.get(k, "Unassigned"))
    yr, mo = extract("year", date_col), extract("month", date_col)
    by_month = [{"month": f"{int(r[0]):04d}-{int(r[1]):02d}", "total": _i(r[2]), "mobilised": _i(r[3])}
                for r in on(db.query(yr, mo, func.count(M.id), _sum_case(M.status == "mobilised")), conds).group_by(yr, mo).order_by(yr, mo).all()]

    # ── Rows ──
    lvl_rank = case((M.escalation_level == "district", 3), (M.escalation_level == "block", 2), else_=1)
    sort_map = {"raised": M.created_at, "name": PW.full_name, "status": M.status, "age": M.created_at, "trigger": M.trigger_type, "level": lvl_rank}
    col = sort_map[p["sort_by"]]
    direction = p["sort_dir"]
    if p["sort_by"] == "age":                    # oldest first == highest age
        direction = "asc" if direction == "desc" else "desc"
    order = [col.desc() if direction == "desc" else col.asc(), M.id.asc()]
    q = on(db.query(M, PW), conds).order_by(*order)
    rows = q.limit(MAX_EXPORT_ROWS).all() if page_size is None else q.offset((page - 1) * page_size).limit(page_size).all()

    blocks, subs, wards = _place_lookup(db, [r[1] for r in rows])
    users = _user_names(db, [r[0].mobilised_by for r in rows])
    cases = []
    for m, pw in rows:
        st = _enum_key(m.status)
        is_open = st in MOB_OPEN
        weeks = gestational_weeks(pw.lmp_date, pw.edd_date, today)
        cases.append({
            "id": m.id, "pregnant_woman_id": pw.id, "full_name": pw.full_name, "mobile_number": pw.mobile_number, "rch_id": pw.rch_id,
            "trigger_type": m.trigger_type, "trigger_label": MOB_TRIGGERS.get(m.trigger_type, m.trigger_type), "trigger_detail": m.trigger_detail,
            "status": st, "escalation_level": m.escalation_level, "created_at": m.created_at,
            "age_days": (today - m.created_at.date()).days if (is_open and m.created_at) else None,
            "escalated_at": m.escalated_at, "mobilised_at": m.mobilised_at, "mobilised_by_name": users.get(m.mobilised_by),
            "hours_to_mobilise": round(max((m.mobilised_at - m.created_at).total_seconds(), 0) / 3600.0, 1) if (m.mobilised_at and m.created_at) else None,
            "remarks": m.remarks, "is_high_risk": bool(pw.is_high_risk), "edd_date": pw.edd_date, "age": pw.age,
            "gestational_weeks": weeks, "block_name": blocks.get(pw.block_id), "sub_centre_name": subs.get(pw.sub_centre_id), "ward_name": wards.get(pw.ward_id),
        })

    return {
        "report_period": {"start_date": start, "end_date": end, "basis": "date mobilised" if p["date_basis"] == "mobilised" else "date case was raised"},
        "filters": {**f.describe(), **{k: v for k, v in p.items() if v not in (None, "", False) and k not in ("sort_by", "sort_dir")}},
        "summary": {
            "total_cases": scoped_total, "matching_filters": total, "unique_women": uniq, "high_risk_women": n_hr,
            "open": open_n, "pending": by_status.get("pending", 0), "escalated": by_status.get("escalated", 0),
            "mobilised": mobilised_n, "auto_closed": closed_n, "ever_escalated": n_ever_escalated,
            "mobilisation_rate": _pct2(mobilised_n, scoped_total),               # closed by a staff member
            "resolved_rate": _pct2(mobilised_n + closed_n, scoped_total),        # staff-mobilised + trigger no longer applies
            "avg_hours_to_mobilise": round(sum(hours) / len(hours), 1) if hours else None,
            "median_hours_to_mobilise": round(median_h, 1) if median_h is not None else None,
        },
        "open_ageing": ageing,
        "status_breakdown": {k: by_status.get(k, 0) for k in ("pending", "escalated", "mobilised", "closed")},
        "breakdowns": {"by_trigger": by_trigger, "by_escalation_level": by_level, "by_block": by_block, "by_sub_centre": by_sub, "by_month": by_month},
        "cases": cases,
        "pagination": _pager(total, page, page_size or total or 1),
        "visualization": generate_visualization_config(
            [{"trigger": r["name"], "count": r["total"]} for r in by_trigger], "bar", "trigger", "count", "Mobilisation Cases by Trigger"),
    }


@router.get("/mobilisation")
async def get_mobilisation_report(
    f: PWFilters = Depends(mob_filters_dep),
    status: Optional[str] = Query(None, description="pending | escalated | mobilised | closed | open (pending + escalated)"),
    trigger_type: Optional[str] = Query(None, description="missed_anc | missed_pmsma | missed_usg | near_edd | hrp_unreferred"),
    escalation_level: Optional[str] = Query(None, description="anm | block | district"),
    date_basis: str = Query("raised", description="raised (case created) | mobilised (date marked mobilised)"),
    older_than_days: Optional[int] = Query(None, ge=1, le=365, description="open cases raised at least this many days ago"),
    search: Optional[str] = None,
    sort_by: str = "raised",
    sort_dir: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Mobilisation report: how many women needed a nudge, why, how fast they were mobilised, what is still open and where it is escalated."""
    if current_user.role not in ("district", "block", "sub_centre"):
        raise HTTPException(status_code=403, detail="District, Block or Sub-centre access required")
    p = _mob_params(status, trigger_type, escalation_level, date_basis, older_than_days, search, sort_by, sort_dir)
    return _run_mobilisation_report(db, current_user, f, p, page, page_size)


@router.get("/mobilisation/export/{format}")
async def export_mobilisation_report(
    format: str,
    f: PWFilters = Depends(mob_filters_dep),
    status: Optional[str] = None,
    trigger_type: Optional[str] = None,
    escalation_level: Optional[str] = None,
    date_basis: str = "raised",
    older_than_days: Optional[int] = Query(None, ge=1, le=365),
    search: Optional[str] = None,
    sort_by: str = "raised",
    sort_dir: str = "desc",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if current_user.role not in ("district", "block", "sub_centre"):
        raise HTTPException(status_code=403, detail="District, Block or Sub-centre access required")
    if format not in ("excel", "csv"):
        raise HTTPException(status_code=400, detail="Format must be 'excel' or 'csv'")
    p = _mob_params(status, trigger_type, escalation_level, date_basis, older_than_days, search, sort_by, sort_dir)
    data = _run_mobilisation_report(db, current_user, f, p, 1, None)
    cols = ["full_name", "mobile_number", "rch_id", "age", "gestational_weeks", "is_high_risk", "edd_date", "trigger_label", "trigger_detail",
            "status", "escalation_level", "created_at", "age_days", "escalated_at", "mobilised_at", "mobilised_by_name", "hours_to_mobilise",
            "remarks", "block_name", "sub_centre_name", "ward_name"]
    rows = [{c: _iso(r[c]) for c in cols} for r in data["cases"]]
    summary = [{"metric": k.replace("_", " ").title(), "value": v} for k, v in data["summary"].items()]
    trig = [{"trigger": r["name"], "total": r["total"], "open": r["open"], "mobilised": r["mobilised"], "auto_closed": r["closed"], "resolved_rate_pct": r["resolved_rate"]}
            for r in data["breakdowns"]["by_trigger"]]
    return await _export(format, {"Mobilisation Cases": rows, "Summary": summary, "By Trigger": trig}, f"mobilisation_{date.today().isoformat()}")


# ═════════════════════════════════════════════════════════════════════════════
# 3. PNC FOLLOW-UP REPORT
# ═════════════════════════════════════════════════════════════════════════════

PNC_SORTS = ("due", "name", "status", "label")
P = PNCReminder
D = Discharge


def _pnc_scope(user: User):
    role = user.role
    if role == "sub_centre" and user.sub_centre_id is not None:
        # reminders carry the sub-centre that was assigned at discharge; the woman's own sub-centre / mapped ward also counts
        return [or_(P.sub_centre_id == user.sub_centre_id, *scope_conditions(user))]
    if role in ("district", "block"):
        return scope_conditions(user)
    return [false()]


def _pnc_params(status, visit_label, dp_id, search, sort_by, sort_dir):
    return {
        "status": _choice(status, PNC_STATUSES, "status"),
        "visit_label": (visit_label or "").strip() or None,
        "dp_id": dp_id,
        "search": (search or "").strip() or None,
        "sort_by": sort_by if sort_by in PNC_SORTS else "due",
        "sort_dir": "asc" if sort_dir == "asc" else "desc",
    }


def _run_pnc_report(db: Session, user: User, f: PWFilters, p: dict, page: int, page_size: Optional[int]):
    today = date.today()
    start, end = _period(f, 30, today)
    open_ = P.status != "completed"
    overdue = and_(open_, P.due_date < today)          # derived from the date: the stored 'due' flag only flips when the scheduler runs

    base = pw_conditions(f, None, include_scope=False) + _pnc_scope(user) + [P.due_date >= start, P.due_date <= end]
    if p["visit_label"]:
        base.append(P.visit_label == p["visit_label"])
    if p["dp_id"]:
        base.append(D.dp_id == p["dp_id"])
    sc = _search_cond(p["search"], [PW.full_name, PW.mobile_number, PW.rch_id])
    if sc is not None:
        base.append(sc)

    conds = list(base)
    st = p["status"]
    if st == "completed":
        conds.append(P.status == "completed")
    elif st == "overdue":
        conds.append(overdue)
    elif st == "due_today":
        conds += [open_, P.due_date == today]
    elif st == "upcoming":
        conds += [open_, P.due_date > today]

    def on(q, cs):
        return q.select_from(P).join(PW, PW.id == P.pregnant_woman_id).join(D, D.id == P.discharge_id).filter(*cs)

    total = _i(on(db.query(func.count(P.id)), conds).scalar())

    n_all, n_done, n_overdue, n_today, n_up, n_women = on(db.query(
        func.count(P.id), _sum_case(P.status == "completed"), _sum_case(overdue),
        _sum_case(and_(open_, P.due_date == today)), _sum_case(and_(open_, P.due_date > today)),
        func.count(func.distinct(P.pregnant_woman_id)),
    ), base).one()
    n_all, n_done, n_overdue, n_today, n_up = map(_i, (n_all, n_done, n_overdue, n_today, n_up))

    on_time, delays = 0, []
    for c_at, due in on(db.query(P.completed_at, P.due_date), base + [P.status == "completed"]).all():
        if c_at is None:
            continue
        late = (c_at.date() - due).days
        if late <= 0:
            on_time += 1
        else:
            delays.append(late)
    with_time = on_time + len(delays)

    aggs = (func.count(P.id), _sum_case(P.status == "completed"), _sum_case(overdue), _sum_case(and_(open_, P.due_date >= today)))

    def split(rows, name_of):
        return sorted([{"key": r[0], "name": name_of(r[0]), "total": _i(r[1]), "completed": _i(r[2]), "overdue": _i(r[3]), "pending": _i(r[4]),
                        "completion_rate": _pct2(_i(r[2]), _i(r[2]) + _i(r[3]))} for r in rows], key=lambda x: -x["total"])

    by_label = split(on(db.query(P.visit_label, *aggs), base).group_by(P.visit_label).all(), lambda k: k)
    by_block, by_sub = [], []
    if user.role == "district" and not f.block_id:
        rows = on(db.query(PW.block_id, *aggs), conds).group_by(PW.block_id).all()
        nm = _names(db, Block, [r[0] for r in rows])
        by_block = split(rows, lambda k: nm.get(k, "Unassigned"))
    if (user.role == "block" or f.block_id) and not f.sub_centre_id:
        rows = on(db.query(PW.sub_centre_id, *aggs), conds).group_by(PW.sub_centre_id).all()
        nm = _names(db, SubCentre, [r[0] for r in rows])
        by_sub = split(rows, lambda k: nm.get(k, "Unassigned"))
    dp_rows = on(db.query(D.dp_id, *aggs), conds).group_by(D.dp_id).all()
    dpn = _names(db, DeliveryPoint, [r[0] for r in dp_rows])
    by_dp = split(dp_rows, lambda k: dpn.get(k, "Unknown"))

    sort_map = {"due": P.due_date, "name": PW.full_name, "status": P.status, "label": P.visit_label}
    col = sort_map[p["sort_by"]]
    q = on(db.query(P, PW, D), conds).order_by(col.desc() if p["sort_dir"] == "desc" else col.asc(), P.id.asc())
    rows = q.limit(MAX_EXPORT_ROWS).all() if page_size is None else q.offset((page - 1) * page_size).limit(page_size).all()

    blocks, subs, wards = _place_lookup(db, [r[1] for r in rows])
    users = _user_names(db, [r[0].completed_by for r in rows])
    dps = _names(db, DeliveryPoint, [r[2].dp_id for r in rows])
    visits = []
    for r, pw, dis in rows:
        done = _enum_key(r.status) == "completed"
        if done:
            eff = "completed"
        elif r.due_date < today:
            eff = "overdue"
        elif r.due_date == today:
            eff = "due_today"
        else:
            eff = "upcoming"
        visits.append({
            "id": r.id, "pregnant_woman_id": pw.id, "full_name": pw.full_name, "mobile_number": pw.mobile_number, "rch_id": pw.rch_id,
            "visit_label": r.visit_label, "due_date": r.due_date, "status": eff,
            "days_overdue": (today - r.due_date).days if eff == "overdue" else 0,
            "completed_at": r.completed_at, "completed_by_name": users.get(r.completed_by),
            "on_time": (r.completed_at.date() <= r.due_date) if (done and r.completed_at) else None,
            "remarks": r.remarks, "discharge_date": dis.discharge_date, "delivery_point_name": dps.get(dis.dp_id),
            "condition_at_discharge": dis.condition_at_discharge, "is_high_risk": bool(pw.is_high_risk), "age": pw.age,
            "block_name": blocks.get(pw.block_id), "sub_centre_name": subs.get(pw.sub_centre_id), "ward_name": wards.get(pw.ward_id),
        })

    return {
        "report_period": {"start_date": start, "end_date": end, "basis": "PNC visit due date"},
        "filters": {**f.describe(), **{k: v for k, v in p.items() if v not in (None, "", False) and k not in ("sort_by", "sort_dir")}},
        "summary": {
            "total_visits": n_all, "matching_filters": total, "women_covered": _i(n_women),
            "completed": n_done, "overdue": n_overdue, "due_today": n_today, "upcoming": n_up,
            "completion_rate": _pct2(n_done, n_done + n_overdue),          # of the visits that have fallen due
            "completed_on_time": on_time, "on_time_rate": _pct2(on_time, with_time),
            "avg_delay_days": round(sum(delays) / len(delays), 1) if delays else 0,
        },
        "breakdowns": {"by_visit_label": by_label, "by_block": by_block, "by_sub_centre": by_sub, "by_delivery_point": by_dp},
        "visits": visits,
        "pagination": _pager(total, page, page_size or total or 1),
        "visualization": generate_visualization_config(
            [{"status": "Completed", "count": n_done}, {"status": "Overdue", "count": n_overdue},
             {"status": "Due today", "count": n_today}, {"status": "Upcoming", "count": n_up}],
            "bar", "status", "count", "PNC Visits by Status"),
    }


@router.get("/pnc-followup")
async def get_pnc_followup_report(
    f: PWFilters = Depends(pnc_filters_dep),
    status: Optional[str] = Query(None, description="upcoming | due_today | overdue | completed"),
    visit_label: Optional[str] = Query(None, description="e.g. 48hr | day7 | day42"),
    dp_id: Optional[int] = Query(None, description="delivery point the woman was discharged from"),
    search: Optional[str] = None,
    sort_by: str = "due",
    sort_dir: str = "asc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Post-natal care follow-up: how many discharge-triggered PNC visits were done, are due, or are overdue."""
    if current_user.role not in ("district", "block", "sub_centre"):
        raise HTTPException(status_code=403, detail="District, Block or Sub-centre access required")
    p = _pnc_params(status, visit_label, dp_id, search, sort_by, sort_dir)
    return _run_pnc_report(db, current_user, f, p, page, page_size)


@router.get("/pnc-followup/export/{format}")
async def export_pnc_followup_report(
    format: str,
    f: PWFilters = Depends(pnc_filters_dep),
    status: Optional[str] = None,
    visit_label: Optional[str] = None,
    dp_id: Optional[int] = None,
    search: Optional[str] = None,
    sort_by: str = "due",
    sort_dir: str = "asc",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if current_user.role not in ("district", "block", "sub_centre"):
        raise HTTPException(status_code=403, detail="District, Block or Sub-centre access required")
    if format not in ("excel", "csv"):
        raise HTTPException(status_code=400, detail="Format must be 'excel' or 'csv'")
    p = _pnc_params(status, visit_label, dp_id, search, sort_by, sort_dir)
    data = _run_pnc_report(db, current_user, f, p, 1, None)
    cols = ["full_name", "mobile_number", "rch_id", "age", "visit_label", "due_date", "status", "days_overdue", "completed_at", "completed_by_name",
            "on_time", "remarks", "discharge_date", "delivery_point_name", "condition_at_discharge", "is_high_risk", "block_name", "sub_centre_name", "ward_name"]
    rows = [{c: _iso(r[c]) for c in cols} for r in data["visits"]]
    summary = [{"metric": k.replace("_", " ").title(), "value": v} for k, v in data["summary"].items()]
    return await _export(format, {"PNC Visits": rows, "Summary": summary}, f"pnc_followup_{date.today().isoformat()}")
