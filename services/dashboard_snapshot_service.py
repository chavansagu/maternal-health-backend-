"""
Dashboard KPI snapshot (District / Block command-center cards + monitoring panels).

One row per (day, district, block) in `dashboard_snapshots`; block_id = 0 means
"whole district". A scheduler job writes today's rows every night, the API only
reads them (and builds today's row on first request if the job has not run yet).
"""
import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import case, func, or_

from models import (
    ANCVisit, Admission, AppointmentStatus, Block, DashboardSnapshot, DeliveryOutcome, DeliveryOutcomeBaby,
    DeliveryReferral, DeliveryType, District, HRPCondition, MaternalOutcome, MobilisationCase,
    PMSMASession, PregnantWoman as PW, USGAppointment,
)
from services.tracking_service import (
    ENHANCED_FROM_WEEK, FULL_TERM_DAYS, NEAR_EDD_DAYS, get_anc_compliance_flags,
)

logger = logging.getLogger(__name__)

# keys == card keys used by DistrictCommandCenter.jsx
KPI_KEYS = [
    "active_pw", "hrp", "weeks_28_plus", "near_edd", "hrp_near_edd",
    "mobilisation_pending", "admitted", "referral_pending", "edd_today",
    "overdue", "maternal_death", "neonatal_infant_death",
]


# monitoring panels under the cards (nested dicts inside the same snapshot)
PANEL_KEYS = ["edd_monitoring","hrp_monitoring","anc_compliance","pmsma_usg","mobilisation_referral"]

# EDD Monitoring buckets: (key, first day, last day) counted from today; None = no upper limit
EDD_BUCKETS = [
    ("today", 0, 0), ("d1_3", 1, 3), ("d4_7", 4, 7), ("d8_10", 8, 10),
    ("d11_15", 11, 15), ("d16_30", 16, 30), ("d30_plus", 31, None),
]


def compute_edd_monitoring(db, district_id, block_id=None, today=None):
    """Still-pregnant women (HRP or not) by how many days are left to their EDD."""
    today = today or date.today()
    conds = [PW.district_id == district_id, PW.is_active == True,  # noqa: E712
             PW.pregnancy_outcome.is_(None), PW.edd_date.isnot(None)]
    if block_id:
        conds.append(PW.block_id == block_id)

    cols = []
    for key, first, last in EDD_BUCKETS:
        cond = PW.edd_date >= today + timedelta(days=first)
        if last is not None:
            cond = cond & (PW.edd_date <= today + timedelta(days=last))
        cols.append(func.coalesce(func.sum(case((cond, 1), else_=0)), 0).label(key))
    row = db.query(*cols).filter(*conds).one()
    return {key: int(row[i]) for i, (key, _, _) in enumerate(EDD_BUCKETS)}

def compute_hrp_monitoring(db, district_id, block_id=None, today=None):
    """HRP Monitoring panel: still-pregnant HRP women, 7 numbers."""
    today = today or date.today()
    near_to = today + timedelta(days=NEAR_EDD_DAYS)
    base = [PW.district_id == district_id, PW.is_active == True,  # noqa: E712
            PW.pregnancy_outcome.is_(None), PW.is_high_risk == True]  # noqa: E712
    if block_id:
        base.append(PW.block_id == block_id)

    def hrp_women(*extra):
        return db.query(func.count(func.distinct(PW.id))).filter(*base, *extra).scalar() or 0

    def hrp_women_via(model, *extra):
        """distinct HRP women who have a row in `model`"""
        return (db.query(func.count(func.distinct(PW.id)))
                .join(model, model.pregnant_woman_id == PW.id)
                .filter(*base, *extra).scalar() or 0)

    return {
        "total": hrp_women(),
        "by_condition": hrp_women_via(HRPCondition, HRPCondition.is_active == True),  # noqa: E712
        "by_block": db.query(func.count(func.distinct(PW.block_id))).filter(*base).scalar() or 0,
        "near_edd": hrp_women(PW.edd_date > today, PW.edd_date <= near_to),
        "not_mobilised": hrp_women_via(MobilisationCase,
                                       MobilisationCase.status.in_(["pending", "escalated"])),
        "admitted": hrp_women_via(Admission, Admission.discharge_date.is_(None)),
        "referred": hrp_women_via(DeliveryReferral),
    }

# ANC Compliance panel rows: (snapshot key, label, list metric for /dashboard/cases)
ANC_PANEL_ROWS = [
    ("completed",  "Completed",     "anc_completed"),
    ("due",        "Due",           "anc_due"),
    ("overdue",    "Overdue",       "anc_overdue"),
    ("w28_done",   "28+ wk done",   "anc_w28_done"),
    ("w28_missed", "28+ wk missed", "anc_w28_missed"),
]
ANC_ROWS = ["completed", "due", "overdue", "w28_done", "w28_missed"]


def anc_compliance_groups(db, district_id, block_id=None, today=None):
    """{row: [woman ids]} for the ANC Compliance panel.
    Still-pregnant women who reached 28 weeks (same 28+ rule as the `weeks_28_plus` card).
    The same result feeds the panel counts and the lists behind each row."""
    today = today or date.today()
    lmp_cut = today - timedelta(weeks=ENHANCED_FROM_WEEK)
    edd_cut = today + timedelta(days=FULL_TERM_DAYS - ENHANCED_FROM_WEEK * 7)
    conds = [PW.district_id == district_id, PW.is_active == True,  # noqa: E712
             PW.pregnancy_outcome.is_(None),
             or_(PW.lmp_date <= lmp_cut,
                 (PW.lmp_date.is_(None)) & (PW.edd_date <= edd_cut))]
    if block_id:
        conds.append(PW.block_id == block_id)

    women = db.query(PW).filter(*conds).all()
    visits = {}
    for pid, vdate in (db.query(ANCVisit.pregnant_woman_id, ANCVisit.visit_date)
                       .join(PW, PW.id == ANCVisit.pregnant_woman_id).filter(*conds)):
        visits.setdefault(pid, []).append(vdate)

    groups = {k: [] for k in ANC_ROWS}
    for w in women:
        flags = get_anc_compliance_flags(w, visits.get(w.id), today)
        for k in ANC_ROWS:
            if flags[k]:
                groups[k].append(w.id)
    return groups


def compute_anc_compliance(db, district_id, block_id=None, today=None):
    """ANC Compliance panel: 5 numbers (women count per row)."""
    groups = anc_compliance_groups(db, district_id, block_id, today)
    return {k: len(v) for k, v in groups.items()}
# PMSMA / USG panel rows: (snapshot key, label, list metric for /dashboard/cases)
PMSMA_USG_ROWS = [
    ("scheduled", "Scheduled", "pmsma_usg_scheduled"),
    ("completed", "Completed", "pmsma_usg_completed"),
    ("pending",   "Pending",   "pmsma_usg_pending"),
    ("hrp_found", "HRP found", "pmsma_usg_hrp_found"),
]
_PMSMA_OPEN = ["scheduled", "rescheduled"]
_USG_OPEN = [AppointmentStatus.SCHEDULED, AppointmentStatus.ACCEPTED, AppointmentStatus.RESCHEDULED]


def pmsma_usg_conditions(key, today):
    """(pmsma_conditions, usg_conditions) of one PMSMA / USG panel row.
    The panel counts visits = PMSMA sessions + USG appointments.
      scheduled : still open (not completed / cancelled) and the date is today or later
      pending   : still open but the date has passed
      completed : status completed
      hrp_found : completed and HRP found (PMSMA: any HRP condition ticked; USG: flagged high risk)
    """
    day_start = datetime.combine(today, datetime.min.time())
    hrp_cols = [getattr(PMSMASession, c.name) == True  # noqa: E712
                for c in PMSMASession.__table__.columns if c.name.startswith("hrp_")]
    if key == "scheduled":
        return ([PMSMASession.status.in_(_PMSMA_OPEN), PMSMASession.scheduled_date >= day_start],
                [USGAppointment.status.in_(_USG_OPEN), USGAppointment.scheduled_date >= day_start])
    if key == "pending":
        return ([PMSMASession.status.in_(_PMSMA_OPEN), PMSMASession.scheduled_date < day_start],
                [USGAppointment.status.in_(_USG_OPEN), USGAppointment.scheduled_date < day_start])
    if key == "completed":
        return ([PMSMASession.status == "completed"],
                [USGAppointment.status == AppointmentStatus.COMPLETED])
    if key == "hrp_found":
        return ([PMSMASession.status == "completed", or_(*hrp_cols)],
                [USGAppointment.status == AppointmentStatus.COMPLETED,
                 USGAppointment.is_high_risk == True])  # noqa: E712
    raise KeyError(key)


def compute_pmsma_usg(db, district_id, block_id=None, today=None):
    """PMSMA / USG panel: 4 numbers (PMSMA sessions + USG appointments)."""
    today = today or date.today()
    scope = [PW.district_id == district_id]
    if block_id:
        scope.append(PW.block_id == block_id)
    out = {}
    for key, _label, _metric in PMSMA_USG_ROWS:
        pm, us = pmsma_usg_conditions(key, today)
        n_pm = (db.query(func.count(PMSMASession.id))
                .join(PW, PW.id == PMSMASession.pregnant_woman_id)
                .filter(*scope, *pm).scalar() or 0)
        n_us = (db.query(func.count(USGAppointment.id))
                .join(PW, PW.id == USGAppointment.pregnant_woman_id)
                .filter(*scope, *us).scalar() or 0)
        out[key] = n_pm + n_us
    return out

# Mobilisation & Referral panel rows: (snapshot key, label, list metric for /dashboard/cases)
MOB_REF_ROWS = [
    ("pending",   "Pending",         "mob_pending"),
    ("planned",   "Planned",         "mob_planned"),
    ("mobilised", "Mobilised",       "mob_mobilised"),
    ("admitted",  "Admitted",        "admitted"),
    ("referral",  "Referral status", "referral_open"),
]
_MOB_OPEN = ["pending", "escalated"]
_REFERRAL_OPEN = ["pending", "accepted"]


def mobilisation_conditions(key):
    """MobilisationCase conditions of one panel row (counts cases).
      pending   : open case (pending / escalated) still at stage pending / contacted
      planned   : open case at stage planned
      mobilised : status mobilised and stage mobilised / reached_facility (not admitted yet)
    Pending + Planned = the 'Mobilisation Pending' card.
    """
    M = MobilisationCase
    if key == "pending":
        return [M.status.in_(_MOB_OPEN), M.mobilisation_stage.in_(["pending", "contacted"])]
    if key == "planned":
        return [M.status.in_(_MOB_OPEN), M.mobilisation_stage == "planned"]
    if key == "mobilised":
        return [M.status == "mobilised", M.mobilisation_stage.in_(["mobilised", "reached_facility"])]
    raise KeyError(key)


def compute_mobilisation_referral(db, district_id, block_id=None, today=None):
    """Mobilisation & Referral panel: 5 numbers.
    admitted = same as the 'Admitted' card; referral = referrals still open (pending / accepted)."""
    mob_scope = [MobilisationCase.district_id == district_id]
    pw_scope = [PW.district_id == district_id]
    if block_id:
        mob_scope.append(MobilisationCase.block_id == block_id)
        pw_scope.append(PW.block_id == block_id)

    out = {k: db.query(func.count(MobilisationCase.id))
              .filter(*mob_scope, *mobilisation_conditions(k)).scalar() or 0
           for k in ("pending", "planned", "mobilised")}
    out["admitted"] = (db.query(func.count(Admission.id))
                       .join(PW, PW.id == Admission.pregnant_woman_id)
                       .filter(*pw_scope, Admission.discharge_date.is_(None)).scalar() or 0)
    out["referral"] = (db.query(func.count(DeliveryReferral.id))
                       .join(PW, PW.id == DeliveryReferral.pregnant_woman_id)
                       .filter(*pw_scope, DeliveryReferral.status.in_(_REFERRAL_OPEN)).scalar() or 0)
    return out
def compute_kpis(db, district_id, block_id=None, today=None):
    """12 cards + monitoring panels."""
    today = today or date.today()
    kpis = _compute_cards(db, district_id, block_id, today)
    kpis["edd_monitoring"] = compute_edd_monitoring(db, district_id, block_id, today)
    kpis["hrp_monitoring"] = compute_hrp_monitoring(db, district_id, block_id, today)
    kpis["anc_compliance"] = compute_anc_compliance(db, district_id, block_id, today)
    kpis["pmsma_usg"] = compute_pmsma_usg(db, district_id, block_id, today)
    kpis["mobilisation_referral"] = compute_mobilisation_referral(db, district_id, block_id, today)
    return kpis


def _compute_cards(db, district_id, block_id=None, today=None):
    today = today or date.today()
    scope = [PW.district_id == district_id]
    if block_id:
        scope.append(PW.block_id == block_id)

    # still pregnant (same definition the EDD engine / enhanced tracking use)
    active = scope + [PW.is_active == True, PW.pregnancy_outcome.is_(None)]  # noqa: E712

    def pw_count(*extra):
        return db.query(func.count(PW.id)).filter(*active, *extra).scalar() or 0

    near_to = today + timedelta(days=NEAR_EDD_DAYS)
    lmp_cut = today - timedelta(weeks=ENHANCED_FROM_WEEK)
    edd_cut = today + timedelta(days=FULL_TERM_DAYS - ENHANCED_FROM_WEEK * 7)

    def via_pw(model, *extra):
        """count rows of `model` whose woman is inside the scope"""
        return (db.query(func.count(model.id))
                .join(PW, PW.id == model.pregnant_woman_id)
                .filter(*scope, *extra).scalar() or 0)

    mob_scope = [MobilisationCase.district_id == district_id]
    if block_id:
        mob_scope.append(MobilisationCase.block_id == block_id)

    babies = (db.query(func.count(DeliveryOutcomeBaby.id))
              .join(DeliveryOutcome, DeliveryOutcome.id == DeliveryOutcomeBaby.outcome_id)
              .join(PW, PW.id == DeliveryOutcome.pregnant_woman_id)
              .filter(*scope, DeliveryOutcomeBaby.status == "infant_death").scalar() or 0)

    return {
        "active_pw": pw_count(),
        "hrp": pw_count(PW.is_high_risk == True),  # noqa: E712
        "weeks_28_plus": pw_count(or_(
            PW.lmp_date <= lmp_cut,
            (PW.lmp_date.is_(None)) & (PW.edd_date <= edd_cut),
        )),
        "near_edd": pw_count(PW.edd_date > today, PW.edd_date <= near_to),
        "hrp_near_edd": pw_count(PW.is_high_risk == True,  # noqa: E712
                                 PW.edd_date > today, PW.edd_date <= near_to),
        "mobilisation_pending": db.query(func.count(MobilisationCase.id)).filter(
            *mob_scope, MobilisationCase.status.in_(["pending", "escalated"])).scalar() or 0,
        "admitted": via_pw(Admission, Admission.discharge_date.is_(None)),
        "referral_pending": via_pw(DeliveryReferral, DeliveryReferral.status == "pending"),
        "edd_today": pw_count(PW.edd_date == today),
        "overdue": pw_count(PW.edd_date < today),
        "maternal_death": via_pw(DeliveryOutcome, or_(
            DeliveryOutcome.maternal_outcome == MaternalOutcome.MATERNAL_DEATH,
            DeliveryOutcome.delivery_type == DeliveryType.MATERNAL_DEATH)),
        "neonatal_infant_death": babies,
    }


def save_snapshot(db, district_id, block_id, metrics, today):
    row = db.query(DashboardSnapshot).filter_by(
        snapshot_date=today, district_id=district_id, block_id=block_id or 0).first()
    if row is None:
        row = DashboardSnapshot(snapshot_date=today, district_id=district_id, block_id=block_id or 0)
        db.add(row)
    row.metrics = json.dumps(metrics)
    row.updated_at = datetime.now()
    db.commit()
    return row


def run_daily_snapshot(db, today=None):
    """Writes today's row for every district and every block. Returns rows written."""
    today = today or date.today()
    written = 0
    for (d_id,) in db.query(District.id).all():
        save_snapshot(db, d_id, 0, compute_kpis(db, d_id, None, today), today)
        written += 1
        for (b_id,) in db.query(Block.id).filter(Block.district_id == d_id).all():
            save_snapshot(db, d_id, b_id, compute_kpis(db, d_id, b_id, today), today)
            written += 1
    return written


def get_snapshot(db, district_id, block_id=None, today=None):
    """Today's snapshot; built on the spot if the nightly job has not produced it yet
    (or if the stored row is older than the current set of panels)."""
    today = today or date.today()
    row = db.query(DashboardSnapshot).filter_by(
        snapshot_date=today, district_id=district_id, block_id=block_id or 0).first()
    stored = json.loads(row.metrics) if row else {}
    if row is None or any(k not in stored for k in PANEL_KEYS):
        row = save_snapshot(db, district_id, block_id, compute_kpis(db, district_id, block_id, today), today)
        stored = json.loads(row.metrics)
    return {
        "snapshot_date": row.snapshot_date.isoformat(),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "metrics": {k: v for k, v in stored.items() if k in KPI_KEYS or k in PANEL_KEYS},
    }