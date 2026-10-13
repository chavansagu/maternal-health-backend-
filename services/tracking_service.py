from datetime import date, datetime,timedelta


def get_tracking_info(pw) -> dict:
    """
    Gestational age is calculated from LMP up to today.
    28+ weeks and still active -> 'enhanced', otherwise 'regular'.
    """
    weeks = None
    if pw is not None and pw.lmp_date:
        lmp = pw.lmp_date.date() if isinstance(pw.lmp_date, datetime) else pw.lmp_date
        weeks = (date.today() - lmp).days // 7

    is_delivered = pw is not None and (
        not pw.is_active or pw.pregnancy_outcome is not None
    )

    tracking_type = (
        "enhanced"
        if (weeks is not None and weeks >= 28 and not is_delivered)
        else "regular"
    )

    return {
        "gestational_weeks": weeks,
        "tracking_type": tracking_type,
    }



# ---------------------------------------------------------------------------
# Enhanced Tracking levels (Step 1)
# NEW helpers only. get_tracking_info() above is untouched because ANC / USG /
# PMSMA routes already use it.
# ---------------------------------------------------------------------------
ENHANCED_FROM_WEEK = 28
FULL_TERM_DAYS = 280
NEAR_EDD_DAYS = 15

LEVEL_LABELS = {
    "critical": "Critical",
    "high_priority": "High priority",
    "enhanced": "Enhanced",
    "high_risk": "High-risk",
    "normal": "Normal",
}
# smaller number = more urgent (used for sorting the list)
LEVEL_ORDER = {"critical": 0, "high_priority": 1, "enhanced": 2, "high_risk": 3, "normal": 4}


def _as_date(value):
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def get_gestational_weeks(pw, today=None):
    """Weeks from LMP; if LMP is missing, from EDD minus 280 days. None if both missing."""
    today = today or date.today()
    lmp = _as_date(pw.lmp_date)
    edd = _as_date(pw.edd_date)
    ref = lmp or (edd - timedelta(days=FULL_TERM_DAYS) if edd else None)
    if ref is None:
        return None
    return max((today - ref).days // 7, 0)


def get_tracking_edd(pw):
    """edd_date; if empty, LMP + 280 days; if both empty, None."""
    edd = _as_date(pw.edd_date)
    if edd:
        return edd
    lmp = _as_date(pw.lmp_date)
    return lmp + timedelta(days=FULL_TERM_DAYS) if lmp else None


def is_delivered(pw) -> bool:
    return (not pw.is_active) or (pw.pregnancy_outcome is not None)


def compute_tracking_level(pw, is_mobilised=False, today=None) -> dict:
    """
    Returns the tracking level of one pregnant woman.
      critical      : HRP + 28 weeks or more + near EDD (or EDD already passed) + not mobilised
      high_priority : HRP + 28 weeks or more
      enhanced      : 28 weeks or more
      high_risk     : HRP, below 28 weeks
      normal        : everything else
    Delivered / inactive women are out of tracking -> level is None.
    is_mobilised  : True when this PW has a mobilisation case with status 'mobilised'.
    """
    today = today or date.today()
    weeks = get_gestational_weeks(pw, today)
    edd = get_tracking_edd(pw)
    days_remaining = (edd - today).days if edd else None

    delivered = is_delivered(pw)
    hrp = bool(pw.is_high_risk)
    enhanced = weeks is not None and weeks >= ENHANCED_FROM_WEEK and not delivered
    near_edd = days_remaining is not None and days_remaining <= NEAR_EDD_DAYS

    if delivered:
        level = None
    elif hrp and enhanced and near_edd and not is_mobilised:
        level = "critical"
    elif hrp and enhanced:
        level = "high_priority"
    elif enhanced:
        level = "enhanced"
    elif hrp:
        level = "high_risk"
    else:
        level = "normal"

    return {
        "level": level,
        "level_label": LEVEL_LABELS.get(level),
        "gestational_weeks": weeks,
        "tracking_edd": edd,
        "days_remaining": days_remaining,
        "is_enhanced": enhanced,
        "is_high_risk": hrp,
        "near_edd": near_edd,
    }



# ---------------------------------------------------------------------------
# Weekly visit expectation (Step 2)
# ---------------------------------------------------------------------------
LAST_EXPECTED_WEEK = 40      # weekly visits are expected until the 40th week
VISIT_GAP_DAYS = 7           # one visit every week
DUE_SOON_DAYS = 2            # "due soon" when the due date is within 2 days

VISIT_STATUS_LABELS = {
    "overdue": "Overdue",
    "due_soon": "Due soon",
    "on_track": "On track",
    "not_applicable": "-",
}


def _reference_date(pw):
    """Date from which weeks are counted: LMP, or EDD minus 280 days."""
    lmp = _as_date(pw.lmp_date)
    if lmp:
        return lmp
    edd = _as_date(pw.edd_date)
    return edd - timedelta(days=FULL_TERM_DAYS) if edd else None


def get_weekly_visit_status(pw, last_anc_date=None, today=None) -> dict:
    """
    Weekly ANC expectation for an enhanced-tracking PW (28th week until the 40th week).
      next_due_visit : last ANC (done in enhanced period) + 7 days. If no ANC has been done since
                       she reached 28 weeks, the visit is due from the day she reached 28 weeks.
                       None when the next due date falls after the 40th week.
      anc_status     : overdue / due_soon / on_track / not_applicable
    last_anc_date  : date of the most recent ANC visit (any), or None.
    """
    today = today or date.today()
    ref = _reference_date(pw)
    result = {"last_anc_date": last_anc_date, "next_due_visit": None,
              "anc_status": "not_applicable", "days_overdue": 0}

    if ref is None or is_delivered(pw):
        return result
    weeks = max((today - ref).days // 7, 0)
    if weeks < ENHANCED_FROM_WEEK:
        return result

    enhanced_start = ref + timedelta(weeks=ENHANCED_FROM_WEEK)
    window_end = ref + timedelta(weeks=LAST_EXPECTED_WEEK)

    last_anc = _as_date(last_anc_date)
    if last_anc and last_anc >= enhanced_start:
        due = last_anc + timedelta(days=VISIT_GAP_DAYS)
    else:
        due = enhanced_start

    if due > window_end:
        return result                      # weekly window over, nothing more expected

    result["next_due_visit"] = due
    if due < today:
        result["anc_status"] = "overdue"
        result["days_overdue"] = (today - due).days
    elif (due - today).days <= DUE_SOON_DAYS:
        result["anc_status"] = "due_soon"
    else:
        result["anc_status"] = "on_track"
    return result   



# ---- EDD buckets (config: yahin se badal sakte hain) ----
# (key, label, min_days, max_days)  -> days_remaining ke hisaab se
EDD_BUCKETS = [
    ("more_than_30", "More than 30 days", 31, None),
    ("30_16", "30-16 days", 16, 30),
    ("15_11", "15-11 days", 11, 15),
    ("10_8", "10-8 days", 8, 10),
    ("7_4", "7-4 days", 4, 7),
    ("3_1", "3-1 days", 1, 3),
    ("edd_today", "EDD today", 0, 0),
    ("overdue", "Overdue", None, -1),
]


def get_edd_bucket(days_remaining):
    if days_remaining is None:
        return None, None
    for key, label, lo, hi in EDD_BUCKETS:
        if (lo is None or days_remaining >= lo) and (hi is None or days_remaining <= hi):
            return key, label
    return None, None


def get_edd_info(pw) -> dict:
    """Tracking EDD (pw.edd_date) ke against days remaining + bucket."""
    def _d(v):
        return v.date() if isinstance(v, datetime) else v

    tracking_edd = _d(pw.edd_date)
    days_remaining = (tracking_edd - date.today()).days if tracking_edd else None
    key, label = get_edd_bucket(days_remaining)

    # delivered / inactive woman ka bucket nahi
    if not pw.is_active or pw.pregnancy_outcome not in (None, "referred"):
        key, label = "delivered", "Delivered"

    return {
        "lmp_edd_date": _d(getattr(pw, "lmp_edd_date", None)),
        "usg_edd_date": _d(getattr(pw, "usg_edd_date", None)),
        "tracking_edd": tracking_edd,
        "edd_source": "USG" if getattr(pw, "usg_edd_date", None) else "LMP",
        "days_remaining": days_remaining,
        "edd_bucket": key,
        "edd_bucket_label": label,
    } 



# Scheduler ke EDD_CASE_TYPES jaisa hi (dono jagah same rakhna)


# Scheduler ke EDD_CASE_TYPES jaisa hi (dono jagah same rakhna)
EDD_ACTION_TYPES = [
    "near_edd",
    "hrp_near_edd",
    "hrp_near_edd_not_mobilised",
    "edd_today_not_admitted",
    "edd_overdue_no_delivery",
]
# mobilisation_routes.DELIVERY_TRIGGERS jaisa hi (stage workflow wale cases)
DELIVERY_STAGE_TRIGGERS = EDD_ACTION_TYPES + ["hrp_unreferred"]


def close_edd_cases(db, pregnant_woman_id, trigger_types=None):
    """Delivery record hone par delivery-related open cases band. Commit caller karega."""
    from models import MobilisationCase

    cases = db.query(MobilisationCase).filter(
        MobilisationCase.pregnant_woman_id == pregnant_woman_id,
        MobilisationCase.trigger_type.in_(trigger_types or DELIVERY_STAGE_TRIGGERS),
        MobilisationCase.status.in_(["pending", "escalated", "mobilised"]),
    ).all()
    for c in cases:
        c.status = "closed"


def close_cases_on_admission(db, pregnant_woman_id, user_id, dp_id, admitted_at=None):
    """
    Admission record hote hi woman ki saari open mobilisation entries band.
    Delivery-related cases ka stage 'admitted' ho jaata hai aur history me row jaati hai.
    Commit caller karega.
    """
    from models import MobilisationCase, MobilisationAction

    now = datetime.now()
    when = admitted_at or now
    cases = db.query(MobilisationCase).filter(
        MobilisationCase.pregnant_woman_id == pregnant_woman_id,
        MobilisationCase.status.in_(["pending", "escalated", "mobilised"]),
    ).all()
    for c in cases:
        if c.trigger_type in DELIVERY_STAGE_TRIGGERS:
            c.mobilisation_stage = "admitted"
            c.current_location = "at_dp"
            c.selected_dp_id = dp_id
            c.stage_updated_at = now
            db.add(MobilisationAction(
                case_id=c.id,
                action_datetime=when,
                mobilisation_stage="admitted",
                current_location="at_dp",
                selected_dp_id=dp_id,
                recorded_by=user_id,
            ))
        c.status = "closed"

    # Required entry done -> close the alerts of these cases (spec M). Commit stays with the caller.
    from services.alert_rules import close_case_alerts
    close_case_alerts(db, [c.id for c in cases], note="Admission recorded", commit=False)


def close_edd_cases(db, pregnant_woman_id, trigger_types=None):
    """Required entry record hote hi open EDD cases band. Commit caller karega."""
    from models import MobilisationCase

    cases = db.query(MobilisationCase).filter(
        MobilisationCase.pregnant_woman_id == pregnant_woman_id,
        MobilisationCase.trigger_type.in_(trigger_types or EDD_ACTION_TYPES),
        MobilisationCase.status.in_(["pending", "escalated"]),
    ).all()
    for c in cases:
        c.status = "closed"

    # Required entry done -> close the alerts of these cases (spec M). Commit stays with the caller.
    from services.alert_rules import close_case_alerts
    close_case_alerts(db, [c.id for c in cases], note="Required entry recorded", commit=False)