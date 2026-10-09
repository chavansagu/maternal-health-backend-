"""
Enhanced Tracking list (28 weeks and above) - read only, nothing is stored.
Levels and weekly visit status come from services/tracking_service.py.
"""
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import and_, or_

from analytics_filters import scope_conditions
from models import (
    ANCVisit, Block, MobilisationCase, PMSMASession, PregnantWoman, SubCentre, USGAppointment,
)
from services.tracking_service import (
    ENHANCED_FROM_WEEK, FULL_TERM_DAYS, LEVEL_ORDER, VISIT_STATUS_LABELS,
    compute_tracking_level, get_weekly_visit_status,
)

TAB_LEVELS = ("enhanced", "high_priority", "critical")


def _enum_value(v):
    return getattr(v, "value", v)


def _enhanced_cohort_conditions(user, block_id, search, today):
    """SQL conditions: in user's jurisdiction, still pregnant, 28 weeks or more."""
    PW = PregnantWoman
    conds = list(scope_conditions(user))
    conds += [PW.is_active == True, PW.pregnancy_outcome.is_(None)]  # noqa: E712
    # 28 weeks reached: LMP is 28 weeks ago or earlier; with no LMP, EDD is 84 days away or less
    lmp_cutoff = today - timedelta(weeks=ENHANCED_FROM_WEEK)
    edd_cutoff = today + timedelta(days=FULL_TERM_DAYS - ENHANCED_FROM_WEEK * 7)
    conds.append(or_(PW.lmp_date <= lmp_cutoff,
                     and_(PW.lmp_date.is_(None), PW.edd_date <= edd_cutoff)))
    if block_id:
        conds.append(PW.block_id == block_id)
    if search and search.strip():
        like = f"%{search.strip()}%"
        conds.append(or_(PW.full_name.ilike(like), PW.mobile_number.ilike(like),
                         PW.abha_id.ilike(like), PW.rch_id.ilike(like)))
    return conds


def _latest_status_map(rows):
    """rows: (pw_id, status) already ordered oldest -> newest; cancelled ones skipped."""
    out = {}
    for pw_id, status in rows:
        status = _enum_value(status)
        if status == "cancelled":
            continue
        out[pw_id] = status
    return out


def get_enhanced_tracking(db, user, level=None, search=None, block_id=None,
                          page=1, per_page=25, today=None):
    today = today or date.today()
    PW = PregnantWoman

    pws = db.query(PW).filter(*_enhanced_cohort_conditions(user, block_id, search, today)).all()
    ids = [p.id for p in pws]

    last_anc, pmsma, usg, mob_cases = {}, {}, {}, {}
    if ids:
        for pw_id, d in (db.query(ANCVisit.pregnant_woman_id, ANCVisit.visit_date)
                         .filter(ANCVisit.pregnant_woman_id.in_(ids))
                         .order_by(ANCVisit.visit_date.asc()).all()):
            last_anc[pw_id] = d
        pmsma = _latest_status_map(
            db.query(PMSMASession.pregnant_woman_id, PMSMASession.status)
            .filter(PMSMASession.pregnant_woman_id.in_(ids))
            .order_by(PMSMASession.scheduled_date.asc()).all())
        usg = _latest_status_map(
            db.query(USGAppointment.pregnant_woman_id, USGAppointment.status)
            .filter(USGAppointment.pregnant_woman_id.in_(ids))
            .order_by(USGAppointment.scheduled_date.asc()).all())
        for pw_id, status in (db.query(MobilisationCase.pregnant_woman_id, MobilisationCase.status)
                              .filter(MobilisationCase.pregnant_woman_id.in_(ids)).all()):
            mob_cases.setdefault(pw_id, set()).add(_enum_value(status))

    block_names = {b.id: b.name for b in db.query(Block.id, Block.name).all()}
    sc_names = {s.id: s.name for s in db.query(SubCentre.id, SubCentre.name).all()}

    items = []
    for pw in pws:
        statuses = mob_cases.get(pw.id, set())
        is_mobilised = "mobilised" in statuses
        if "pending" in statuses or "escalated" in statuses:
            mob_status = "escalated" if "escalated" in statuses else "pending"
        elif is_mobilised:
            mob_status = "mobilised"
        elif "closed" in statuses:
            mob_status = "closed"
        else:
            mob_status = "none"

        t = compute_tracking_level(pw, is_mobilised=is_mobilised, today=today)
        if t["level"] not in TAB_LEVELS:
            continue
        v = get_weekly_visit_status(pw, last_anc.get(pw.id), today=today)
        items.append({
            "id": pw.id,
            "full_name": pw.full_name,
            "mobile_number": pw.mobile_number,
            "block_name": block_names.get(pw.block_id),
            "sub_centre_name": sc_names.get(pw.sub_centre_id),
            "gestational_weeks": t["gestational_weeks"],
            "last_anc_date": v["last_anc_date"],
            "next_due_visit": v["next_due_visit"],
            "anc_status": v["anc_status"],
            "anc_status_label": VISIT_STATUS_LABELS[v["anc_status"]],
            "days_overdue": v["days_overdue"],
            "is_high_risk": t["is_high_risk"],
            "pmsma_status": pmsma.get(pw.id, "none"),
            "usg_status": usg.get(pw.id, "none"),
            "tracking_edd": t["tracking_edd"],
            "days_remaining": t["days_remaining"],
            "mobilisation_status": mob_status,
            "level": t["level"],
            "level_label": t["level_label"],
        })

    summary = {
        "total": len(items),
        "enhanced": sum(1 for i in items if i["level"] == "enhanced"),
        "high_priority": sum(1 for i in items if i["level"] == "high_priority"),
        "critical": sum(1 for i in items if i["level"] == "critical"),
        "anc_overdue": sum(1 for i in items if i["anc_status"] == "overdue"),
    }

    if level in TAB_LEVELS:
        items = [i for i in items if i["level"] == level]
    # most urgent first, then the ones closest to delivery
    items.sort(key=lambda i: (LEVEL_ORDER[i["level"]],
                              i["days_remaining"] if i["days_remaining"] is not None else 9999))

    total = len(items)
    page = max(page, 1)
    per_page = max(min(per_page, 200), 1)
    start = (page - 1) * per_page
    return {
        "summary": summary,
        "items": items[start:start + per_page],
        "total": total,
        "page": page,
        "per_page": per_page,
    }