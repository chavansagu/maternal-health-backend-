"""
APScheduler - Automatic Job Scheduler
Runs background jobs at scheduled intervals without external cron setup.
Currently handles:
  - High-risk advisory IVR calls (daily at 9 AM)
  - Feedback Call 1 (4 times per day: 10 AM, 12 PM, 2 PM, 4 PM)
  - Feedback Call 2 (4 times per day: 10:30 AM, 12:30 PM, 2:30 PM, 4:30 PM)
  - Mobilisation case generation/escalation (daily / every 4 hours)
  - PNC reminder surfacing (daily at 6:30 AM)
"""
import os
import requests
import logging
from datetime import datetime, timedelta
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Read config from .env
API_BASE_URL = os.getenv("SCHEDULER_API_BASE_URL", "http://localhost:8000")
CRON_SECRET = os.getenv("CRON_SECRET", "change-this-secret-in-production")

# Scheduler instance (single instance for the whole app)
scheduler = BackgroundScheduler()


def check_reschedule_window(
    new_date: datetime,
    original_date: datetime,
    is_emergency_override: bool,
    override_reason: Optional[str],
) -> dict:
    """
    Shared reschedule validation for both USG centre and PMSMA reschedule paths.

    - Within 7 days of original_date: allowed normally.
    - Beyond 7 days: only allowed as emergency override with a mandatory reason.

    Returns dict: {"allowed": True, "is_override": bool, "reason": str|None}
    Raises HTTPException(400) on violation.
    """
    from fastapi import HTTPException
    days_diff = abs((new_date - original_date).days)
    within_window = days_diff <= 7

    if within_window:
        return {"allowed": True, "is_override": False, "reason": None}

    if not is_emergency_override:
        raise HTTPException(
            status_code=400,
            detail=(
                "Rescheduling must be within 7 days of original date. "
                # "To override, set is_emergency_override=true and provide override_reason."
            )
        )
    if not override_reason or not override_reason.strip():
        raise HTTPException(
            status_code=400,
            detail="override_reason is required when bypassing the 7-day notice period."
        )
    return {"allowed": True, "is_override": True, "reason": override_reason.strip()}


def job_high_risk_advisory_calls():
    """
    Job: Trigger high-risk advisory IVR calls for all active high-risk women.
    Runs daily at 9:00 AM.
    Skips women who were called in the last 7 days.
    """
    logger.info(f"[Scheduler] High-risk advisory IVR job started at {datetime.now()}")
    try:
        from database import SessionLocal
        from models import PregnantWoman, IVRCallLog
        from ivr_service import trigger_high_risk_call

        db = SessionLocal()
        seven_days_ago = datetime.now() - timedelta(days=7)
        try:
            high_risk_women = db.query(PregnantWoman).filter(
                PregnantWoman.is_high_risk == True,
                PregnantWoman.is_active == True
            ).all()

            triggered = skipped = failed = 0

            for pw in high_risk_women:
                recent_call = db.query(IVRCallLog).filter(
                    IVRCallLog.pregnant_woman_id == pw.id,
                    IVRCallLog.call_type == "high_risk_advisory",
                    IVRCallLog.created_at >= seven_days_ago
                ).first()

                if recent_call:
                    skipped += 1
                    continue

                result = trigger_high_risk_call(pw.mobile_number, pw.id)

                call_log = IVRCallLog(
                    pregnant_woman_id=pw.id,
                    mobile_number=pw.mobile_number,
                    call_type="high_risk_advisory",
                    call_status="initiated" if result["success"] else "failed",
                    ivr_campaign_id=result.get("campaign_id"),
                    provider_response=result.get("raw_response"),
                    called_at=datetime.now() if result["success"] else None,
                )
                db.add(call_log)
                triggered += 1 if result["success"] else 0
                failed += 1 if not result["success"] else 0

            db.commit()
            logger.info(
                f"[Scheduler] High-risk IVR DONE — "
                f"triggered={triggered}, skipped={skipped}, failed={failed}"
            )
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Scheduler] High-risk IVR FAILED — {str(e)}")


def job_feedback_call_1():
    """
    Job: Trigger Feedback Call 1 for completed USG appointments.
    Runs 4 times per day: 10 AM, 12 PM, 2 PM, 4 PM.

    Logic: No tight time window — picks ANY completed appointment that:
      1. completed_date <= now - 24h  (at least 24 hours ago)
      2. No feedback_call_1 log exists for it yet
      3. pregnant_woman is active

    This guarantees NO appointment is ever missed regardless of completion time.
    Dedup check ensures each appointment is called exactly once.
    """
    logger.info(f"[Scheduler] Feedback Call 1 job started at {datetime.now()}")
    try:
        from database import SessionLocal
        from models import USGAppointment, PregnantWoman, IVRCallLog
        from sqlalchemy import exists
        from ivr_service import trigger_feedback_call_1

        db = SessionLocal()
        now = datetime.now()
        cutoff = now - timedelta(hours=24)

        try:
            # Get ALL completed appointments that are at least 24h old
            # and have never received a feedback_call_1
            completed_appointments = db.query(USGAppointment).join(PregnantWoman).filter(
                USGAppointment.status == "completed",
                USGAppointment.completed_date <= cutoff,
                PregnantWoman.is_active == True,
                ~exists().where(
                    (IVRCallLog.pregnant_woman_id == USGAppointment.pregnant_woman_id) &
                    (IVRCallLog.call_type == "feedback_call_1") &
                    (IVRCallLog.created_at >= USGAppointment.completed_date)
                )
            ).all()

            triggered = skipped = failed = 0

            for appointment in completed_appointments:
                result = trigger_feedback_call_1(
                    appointment.pregnant_woman.mobile_number,
                    appointment.pregnant_woman_id,
                    appointment.id
                )

                call_log = IVRCallLog(
                    pregnant_woman_id=appointment.pregnant_woman_id,
                    mobile_number=appointment.pregnant_woman.mobile_number,
                    call_type="feedback_call_1",
                    call_status="initiated" if result["success"] else "failed",
                    ivr_campaign_id=result.get("campaign_id"),
                    provider_response=result.get("raw_response"),
                    called_at=datetime.now() if result["success"] else None,
                )
                db.add(call_log)

                triggered += 1 if result["success"] else 0
                failed += 1 if not result["success"] else 0

            db.commit()
            logger.info(
                f"[Scheduler] Feedback Call 1 DONE — "
                f"triggered={triggered}, skipped={skipped}, failed={failed}"
            )
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Scheduler] Feedback Call 1 FAILED — {str(e)}")


def job_feedback_call_2():
    """
    Job: Trigger Feedback Call 2 for failed/no-response Call 1s.
    Runs 4 times per day during business hours: 10:30 AM, 12:30 PM, 2:30 PM, 4:30 PM.
    Triggers calls for Call 1s made 48+ hours ago that failed or got no response.
    """
    logger.info(f"[Scheduler] Feedback Call 2 job started at {datetime.now()}")
    try:
        from database import SessionLocal
        from models import IVRCallLog, PregnantWoman
        from ivr_service import trigger_feedback_call_2

        db = SessionLocal()
        now = datetime.now()
        
        # Time window: 48+ hours ago
        cutoff_time = now - timedelta(hours=48)
        
        try:
            # Get Call 1s that need follow-up (failed, no_answer, busy)
            failed_call_1s = db.query(IVRCallLog).join(PregnantWoman).filter(
                IVRCallLog.call_type == "feedback_call_1",
                IVRCallLog.created_at <= cutoff_time,
                IVRCallLog.call_status.in_(["failed", "no_answer", "busy"]),
                PregnantWoman.is_active == True
            ).all()

            triggered = skipped = failed = 0

            for call_1_log in failed_call_1s:
                # Check if Call 2 already made for this woman
                existing_call_2 = db.query(IVRCallLog).filter(
                    IVRCallLog.pregnant_woman_id == call_1_log.pregnant_woman_id,
                    IVRCallLog.call_type == "feedback_call_2",
                    IVRCallLog.created_at >= call_1_log.created_at
                ).first()

                if existing_call_2:
                    skipped += 1
                    continue

                # Trigger feedback call 2
                result = trigger_feedback_call_2(
                    call_1_log.mobile_number,
                    call_1_log.pregnant_woman_id,
                    None  # We don't have direct USG appointment reference from Call 1 log
                )

                # Log the call
                call_log = IVRCallLog(
                    pregnant_woman_id=call_1_log.pregnant_woman_id,
                    mobile_number=call_1_log.mobile_number,
                    call_type="feedback_call_2",
                    call_status="initiated" if result["success"] else "failed",
                    ivr_campaign_id=result.get("campaign_id"),
                    provider_response=result.get("raw_response"),
                    called_at=datetime.now() if result["success"] else None,
                )
                db.add(call_log)
                
                triggered += 1 if result["success"] else 0
                failed += 1 if not result["success"] else 0

            db.commit()
            logger.info(
                f"[Scheduler] Feedback Call 2 DONE — "
                f"triggered={triggered}, skipped={skipped}, failed={failed}"
            )
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Scheduler] Feedback Call 2 FAILED — {str(e)}")


def _upsert_mobilisation_case(db, pw, trigger_type, detail, reference_id=None,
                              priority=None, pending_action=None,
                              responsible_role="sub_centre"):
    """
    Create a mobilisation case if there isn't already an OPEN one
    (pending/escalated) for this woman + trigger type.
    """
    from models import MobilisationCase

    existing = db.query(MobilisationCase).filter(
        MobilisationCase.pregnant_woman_id == pw.id,
        MobilisationCase.trigger_type == trigger_type,
        MobilisationCase.status.in_(["pending", "escalated"]),
    ).first()

    if existing:
        # Refresh the detail text (e.g. "EDD in 12 days" -> "EDD in 9 days")
        existing.trigger_detail = detail
        if priority:
            existing.priority = priority
        if pending_action:
            existing.pending_action = pending_action
        return None

    new_case = MobilisationCase(
        pregnant_woman_id=pw.id,
        trigger_type=trigger_type,
        trigger_reference_id=reference_id,
        trigger_detail=detail,
        priority=priority or "normal",
        pending_action=pending_action or detail,
        responsible_role=responsible_role,
        sub_centre_id=pw.sub_centre_id,
        block_id=pw.block_id,
        district_id=pw.district_id,
        status="pending",
        escalation_level="anm",
    )
    db.add(new_case)
    db.flush()   # id mil jaaye
        # Mobilisation pending -> Sub-centre + Block (alert matrix, spec M)
    # EDD cases ka alert _notify_edd_case() alag se bhejta hai, isliye yahan skip.
    if trigger_type not in EDD_CASE_TYPES:
        from services.alert_rules import raise_alert
        db.commit()  # case save ho jaye, taaki raise_alert ka rollback ise na hataye
        raise_alert(
            db, "mobilisation_pending", pw,
            title="Mobilisation pending",
            message=f"{pw.full_name}: {detail}. Action: {new_case.pending_action}",
            alert_key=f"mobcase:{new_case.id}",
            reference_id=new_case.id,
            reference_type="MobilisationCase",
            action_url="/mobilisation-management",
            metadata={"pregnant_woman_name": pw.full_name, "trigger_type": trigger_type},
        )
    return new_case
def _auto_close_resolved_mobilisation_cases(db):
    """
    Close open cases whose underlying trigger no longer applies
    (e.g. the ANC visit got recorded, the PW delivered, referral was made).
    """
    from models import MobilisationCase, PregnantWoman, ANCVisit, PMSMASession, USGAppointment, DeliveryReferral

    open_cases = db.query(MobilisationCase).filter(
        MobilisationCase.status.in_(["pending", "escalated"])
    ).all()

    for case in open_cases:
        pw = db.query(PregnantWoman).get(case.pregnant_woman_id)
        if not pw or not pw.is_active or pw.pregnancy_outcome:
            case.status = "closed"
            continue

        if case.trigger_type == "near_edd" and pw.pregnancy_outcome:
            case.status = "closed"

        elif case.trigger_type == "missed_anc":
            # closed if a newer ANC visit exists than when the case was raised
            recent_visit = db.query(ANCVisit).filter(
                ANCVisit.pregnant_woman_id == pw.id,
                ANCVisit.visit_date >= case.created_at.date(),
            ).first()
            if recent_visit:
                case.status = "closed"

        elif case.trigger_type == "missed_pmsma":
            done = db.query(PMSMASession).filter(
                PMSMASession.pregnant_woman_id == pw.id,
                PMSMASession.status == "completed",
                PMSMASession.updated_at >= case.created_at,
            ).first()
            if done:
                case.status = "closed"

        elif case.trigger_type == "missed_usg":
            done = db.query(USGAppointment).filter(
                USGAppointment.pregnant_woman_id == pw.id,
                USGAppointment.status == "completed",
                USGAppointment.updated_at >= case.created_at,
            ).first()
            if done:
                case.status = "closed"

        elif case.trigger_type == "hrp_unreferred":
            if not pw.is_high_risk:
                case.status = "closed"
            else:
                referred = db.query(DeliveryReferral).filter(
                    DeliveryReferral.pregnant_woman_id == pw.id
                ).first()
                if referred:
                    case.status = "closed"

    # Alert closure on completion of the required entry (spec M)
    from services.alert_rules import close_case_alerts
    close_case_alerts(db, [c.id for c in open_cases if c.status == "closed"],
                      note="Mobilisation case no longer applies", commit=False)


def job_generate_mobilisation_cases():
    """
    Job: Scan existing signals (ANC/PMSMA/USG due-lists, near-EDD, unreferred HRP)
    and create/refresh mobilisation worklist entries.
    Runs once daily. No SMS/call/AI action — this only populates the tracking list.
    """
    logger.info(f"[Scheduler] Mobilisation case generation started at {datetime.now()}")
    try:
        from database import SessionLocal
        from models import PregnantWoman, ANCVisit, PMSMASession, USGAppointment, DeliveryReferral

        db = SessionLocal()
        today = datetime.now().date()
        now = datetime.now()

        try:
            active_pws = db.query(PregnantWoman).filter(
                PregnantWoman.is_active == True,
                PregnantWoman.pregnancy_outcome.is_(None),
            ).all()

            # 1) Near-EDD — within 15 days
            # near_edd_cutoff = today + timedelta(days=15)
            # for pw in active_pws:
            #     if pw.edd_date and today <= pw.edd_date <= near_edd_cutoff:
            #         days_left = (pw.edd_date - today).days
            #         _upsert_mobilisation_case(
            #             db, pw, "near_edd", f"EDD in {days_left} day(s)"
            #         )

            # 2) Missed ANC — next_visit_date passed with no later visit recorded
            overdue_anc_visits = db.query(ANCVisit).filter(
                ANCVisit.next_visit_date.isnot(None),
                ANCVisit.next_visit_date < today,
            ).all()
            seen_pw_ids = set()
            for visit in overdue_anc_visits:
                if visit.pregnant_woman_id in seen_pw_ids:
                    continue
                later_visit = db.query(ANCVisit).filter(
                    ANCVisit.pregnant_woman_id == visit.pregnant_woman_id,
                    ANCVisit.visit_date > visit.visit_date,
                ).first()
                if later_visit:
                    continue
                pw = db.query(PregnantWoman).get(visit.pregnant_woman_id)
                if pw and pw.is_active and not pw.pregnancy_outcome:
                    days_overdue = (today - visit.next_visit_date).days
                    _upsert_mobilisation_case(
                        db, pw, "missed_anc",
                        f"ANC visit overdue by {days_overdue} day(s)",
                        reference_id=visit.id,
                    )
                    seen_pw_ids.add(visit.pregnant_woman_id)

            # 3) Missed PMSMA — scheduled/rescheduled session in the past, not completed
            overdue_pmsma = db.query(PMSMASession).filter(
                PMSMASession.status.in_(["scheduled", "rescheduled"]),
                PMSMASession.scheduled_date < now,
            ).all()
            for session in overdue_pmsma:
                pw = db.query(PregnantWoman).get(session.pregnant_woman_id)
                if pw and pw.is_active and not pw.pregnancy_outcome:
                    days_overdue = (now - session.scheduled_date).days
                    _upsert_mobilisation_case(
                        db, pw, "missed_pmsma",
                        f"PMSMA session overdue by {days_overdue} day(s)",
                        reference_id=session.id,
                    )

            # 4) Missed USG — scheduled/accepted/rescheduled appointment in the past
            overdue_usg = db.query(USGAppointment).filter(
                USGAppointment.status.in_(["scheduled", "accepted", "rescheduled"]),
                USGAppointment.scheduled_date < now,
            ).all()
            for appt in overdue_usg:
                pw = db.query(PregnantWoman).get(appt.pregnant_woman_id)
                if pw and pw.is_active and not pw.pregnancy_outcome:
                    days_overdue = (now - appt.scheduled_date).days
                    _upsert_mobilisation_case(
                        db, pw, "missed_usg",
                        f"USG appointment overdue by {days_overdue} day(s)",
                        reference_id=appt.id,
                    )

            # 5) HRP not yet referred to a delivery point
            hrp_unreferred = db.query(PregnantWoman).filter(
                PregnantWoman.is_active == True,
                PregnantWoman.is_high_risk == True,
                PregnantWoman.pregnancy_outcome.is_(None),
            ).all()
            for pw in hrp_unreferred:
                has_referral = db.query(DeliveryReferral).filter(
                    DeliveryReferral.pregnant_woman_id == pw.id
                ).first()
                if not has_referral:
                    _upsert_mobilisation_case(
                        db, pw, "hrp_unreferred", "High-risk case not yet referred to a delivery point",
                        priority="high",
                    )

            # 6) Close cases whose trigger has resolved itself
            _auto_close_resolved_mobilisation_cases(db)

            db.commit()
            logger.info("[Scheduler] Mobilisation case generation DONE")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Scheduler] Mobilisation case generation FAILED — {str(e)}")
# Mobilisation EDD case type -> alert matrix event (services/alert_rules.py)
EDD_TRIGGER_TO_EVENT = {
    "near_edd": "edd_approaching",
    "hrp_near_edd": "hrp_near_edd",
    "hrp_near_edd_not_mobilised": "hrp_near_edd",
    "edd_today_not_admitted": "edd_today_not_admitted",
    "edd_overdue_no_delivery": "edd_crossed_no_delivery",
}

EDD_ALERT_TITLES = {
    "near_edd": "Near EDD reminder",
    "hrp_near_edd": "HRP near EDD alert",
    "hrp_near_edd_not_mobilised": "RED FLAG: immediate mobilisation required",
    "edd_today_not_admitted": "RED FLAG: EDD today, not admitted",
    "edd_overdue_no_delivery": "CRITICAL: EDD crossed, no delivery recorded",
}


def _notify_edd_case(db, pw, case):
    """Naye EDD case par alert matrix ke hisaab se alert (recipients matrix decide karta hai)."""
    from services.alert_rules import raise_alert

    event_type = EDD_TRIGGER_TO_EVENT.get(case.trigger_type)
    if not event_type:
        return

    # Red flag / critical cases: sabko urgent (matrix ka role_priority sirf plain "HRP near EDD" ke liye hai)
    force_priority = "urgent" if case.priority in ("red_flag", "critical") else None

    raise_alert(
        db, event_type, pw,
        title=EDD_ALERT_TITLES.get(case.trigger_type, "EDD alert"),
        message=f"{pw.full_name}: {case.trigger_detail}. Action: {case.pending_action}",
        alert_key=f"mobcase:{case.id}",
        reference_id=case.id,
        reference_type="MobilisationCase",
        action_url="/mobilisation-management",
        metadata={"pregnant_woman_name": pw.full_name, "trigger_type": case.trigger_type},
        priority=force_priority,
    )

EDD_CASE_TYPES = [
    "near_edd",
    "hrp_near_edd",
    "hrp_near_edd_not_mobilised",
    "edd_today_not_admitted",
    "edd_overdue_no_delivery",
]


def job_generate_edd_action_cases():
    """
    Job: EDD-based action engine. Har active woman ke liye sirf ek sabse
    serious EDD case rakhta hai (priority logic):
      EDD crossed + no delivery        -> critical
      EDD today + not admitted         -> red_flag
      HRP + near EDD + ANC + not mobilised -> red_flag
      HRP + near EDD                   -> high
      Near EDD                         -> normal
    "Near EDD" = 1 to 15 days remaining. Runs daily.
    """
    logger.info(f"[Scheduler] EDD action engine started at {datetime.now()}")
    try:
        from database import SessionLocal
        from models import PregnantWoman, ANCVisit, Admission, MobilisationCase

        db = SessionLocal()
        today = datetime.now().date()
        try:
            pws = db.query(PregnantWoman).filter(
                PregnantWoman.is_active == True,
                PregnantWoman.pregnancy_outcome.is_(None),
                PregnantWoman.edd_date.isnot(None),
            ).all()

            for pw in pws:
                days = (pw.edd_date - today).days
                trigger = None  # (type, priority, detail, pending_action)

                if days < 0:
                    trigger = (
                        "edd_overdue_no_delivery", "critical",
                        f"EDD crossed {abs(days)} day(s) ago - no delivery recorded",
                        "Locate the woman and record delivery / admission",
                    )
                elif days == 0:
                    admitted = db.query(Admission.id).filter(
                        Admission.pregnant_woman_id == pw.id
                    ).first()
                    if not admitted:
                        trigger = (
                            "edd_today_not_admitted", "red_flag",
                            "EDD is today - woman not admitted",
                            "Admit to delivery point immediately",
                        )
                elif days <= 15:
                    if pw.is_high_risk:
                        at_anc = db.query(ANCVisit.id).filter(
                            ANCVisit.pregnant_woman_id == pw.id
                        ).first()
                        mobilised = db.query(MobilisationCase.id).filter(
                            MobilisationCase.pregnant_woman_id == pw.id,
                            MobilisationCase.status == "mobilised",
                        ).first()
                        if at_anc and not mobilised:
                            trigger = (
                                "hrp_near_edd_not_mobilised", "red_flag",
                                f"HRP - EDD in {days} day(s), at ANC, not mobilised",
                                "Immediate mobilisation required",
                            )
                        else:
                            trigger = (
                                "hrp_near_edd", "high",
                                f"HRP - EDD in {days} day(s)",
                                "Confirm delivery plan and facility for high-risk woman",
                            )
                    else:
                        trigger = (
                            "near_edd", "normal",
                            f"EDD in {days} day(s)",
                            "Remind woman and confirm delivery plan",
                        )

                keep_type = trigger[0] if trigger else None

                # Is woman ke baaki open EDD cases band (severity badli ya condition khatam)
                stale = db.query(MobilisationCase).filter(
                    MobilisationCase.pregnant_woman_id == pw.id,
                    MobilisationCase.trigger_type.in_(EDD_CASE_TYPES),
                    MobilisationCase.status.in_(["pending", "escalated"]),
                    MobilisationCase.trigger_type != (keep_type or ""),
                ).all()
                for c in stale:
                    c.status = "closed"
                if stale:
                    from services.alert_rules import close_case_alerts
                    close_case_alerts(db, [c.id for c in stale],
                                      note="EDD situation changed", commit=False)

                if not trigger:
                    continue

                t_type, t_priority, t_detail, t_action = trigger

                # Normal/high cases: agar ANM is woman ko kisi bhi EDD case me mobilise kar chuki hai
                # (red-flag ho ya normal), to dobara naya case na banao
                if t_priority in ("normal", "high"):
                    done = db.query(MobilisationCase.id).filter(
                        MobilisationCase.pregnant_woman_id == pw.id,
                        MobilisationCase.trigger_type.in_(EDD_CASE_TYPES),
                        MobilisationCase.status == "mobilised",
                    ).first()
                    if done:
                        continue

                new_case = _upsert_mobilisation_case(
                    db, pw, t_type, t_detail,
                    priority=t_priority, pending_action=t_action,
                    responsible_role="sub_centre",
                )
                if new_case:
                    _notify_edd_case(db, pw, new_case)

            db.commit()
            logger.info("[Scheduler] EDD action engine DONE")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Scheduler] EDD action engine FAILED — {str(e)}")

def job_escalate_mobilisation_cases():
    """
    Job: Escalate mobilisation cases that have sat open too long.
    anm -> block after 3 days pending, block -> district after another 3 days.
    Runs every few hours. No SMS/call/AI action — just moves the case up
    the review chain so it appears on the next role's dashboard.
    """
    logger.info(f"[Scheduler] Mobilisation escalation started at {datetime.now()}")
    try:
        from database import SessionLocal
        from models import MobilisationCase

        db = SessionLocal()
        try:
            anm_cutoff = datetime.now() - timedelta(days=3)
            block_cutoff = datetime.now() - timedelta(days=6)

            to_block = db.query(MobilisationCase).filter(
                MobilisationCase.status.in_(["pending", "escalated"]),
                MobilisationCase.escalation_level == "anm",
                MobilisationCase.created_at <= anm_cutoff,
            ).all()
            for case in to_block:
                case.escalation_level = "block"
                case.status = "escalated"
                case.escalated_at = datetime.now()

            to_district = db.query(MobilisationCase).filter(
                MobilisationCase.status == "escalated",
                MobilisationCase.escalation_level == "block",
                MobilisationCase.created_at <= block_cutoff,
            ).all()
            for case in to_district:
                case.escalation_level = "district"
                case.escalated_at = datetime.now()

            db.commit()

            # Keep the notification / escalation_logs side in step with the case chain (spec M)
            from services.alert_rules import escalate_alert
            for case in to_block:
                escalate_alert(db, f"mobcase:{case.id}", "block",
                               note="Mobilisation case pending > 3 days")
            for case in to_district:
                escalate_alert(db, f"mobcase:{case.id}", "district",
                               note="Mobilisation case pending > 6 days")

            logger.info(
                f"[Scheduler] Mobilisation escalation DONE — "
                f"to_block={len(to_block)}, to_district={len(to_district)}"
            )
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Scheduler] Mobilisation escalation FAILED — {str(e)}")


def job_surface_pnc_reminders():
    """
    Job: Flip PNC reminders from 'scheduled' to 'due' once their due_date has
    arrived, so they appear on the ANM/sub-centre worklist (see
    routes/pnc_reminder_routes.py). Runs once daily. No SMS/call/AI action —
    same pattern as the mobilisation jobs: this only surfaces the worklist item.
    """
    logger.info(f"[Scheduler] PNC reminder surfacing started at {datetime.now()}")
    try:
        from database import SessionLocal
        from models import PNCReminder

        db = SessionLocal()
        today = datetime.now().date()

        try:
            due_now = db.query(PNCReminder).filter(
                PNCReminder.status == "scheduled",
                PNCReminder.due_date <= today,
            ).all()

            for reminder in due_now:
                reminder.status = "due"

            db.commit()
            logger.info(f"[Scheduler] PNC reminder surfacing DONE — surfaced={len(due_now)}")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Scheduler] PNC reminder surfacing FAILED — {str(e)}")

ANC_DUE_WINDOW_DAYS = 2     # next ANC date within 2 days = "due"
PMSMA_DUE_WINDOW_DAYS = 2   # PMSMA session within 2 days = "due"


def job_generate_anc_pmsma_alerts():
    """
    Job: ANC / PMSMA time-based alerts (alert matrix, spec M). Runs daily.
      Below 28 weeks (regular ANC, last visit's next_visit_date):
          due within 2 days   -> anc_due        (Sub-centre)
          date passed         -> anc_overdue    (Sub-centre + Block)
      28 weeks and above (weekly visits, tracking_service.get_weekly_visit_status):
          due soon            -> anc_visit_due     (Sub-centre)
          overdue             -> anc_visit_missed  (Sub-centre; Block comes via escalation)
      PMSMA session (scheduled / rescheduled) within 2 days -> pmsma_due (Sub-centre + PMSMA)
    raise_alert() skips users who already have the same alert open (alert_key),
    so the same alert is not repeated every day.
    """
    logger.info(f"[Scheduler] ANC/PMSMA alerts started at {datetime.now()}")
    try:
        from database import SessionLocal
        from models import PregnantWoman, ANCVisit, PMSMASession
        from services.alert_rules import raise_alert
        from services.tracking_service import get_gestational_weeks, get_weekly_visit_status, ENHANCED_FROM_WEEK

        db = SessionLocal()
        today = datetime.now().date()
        counts = {}
        try:
            pws = db.query(PregnantWoman).filter(
                PregnantWoman.is_active == True,
                PregnantWoman.pregnancy_outcome.is_(None),
            ).all()
            pw_map = {pw.id: pw for pw in pws}

            # Latest ANC visit of each woman (single query)
            last_visit = {}
            for v in db.query(ANCVisit).order_by(ANCVisit.visit_date.asc(), ANCVisit.visit_number.asc()).all():
                if v.pregnant_woman_id in pw_map:
                    last_visit[v.pregnant_woman_id] = v

            for pw in pws:
                visit = last_visit.get(pw.id)
                event = title = msg = None

                weeks = get_gestational_weeks(pw, today)
                if weeks is not None and weeks >= ENHANCED_FROM_WEEK:
                    # 28+ weeks: weekly visits
                    weekly = get_weekly_visit_status(pw, visit.visit_date if visit else None, today)
                    due = weekly["next_due_visit"]
                    if weekly["anc_status"] == "overdue":
                        event, title = "anc_visit_missed", "Weekly ANC visit missed"
                        msg = f"{pw.full_name}: weekly ANC visit was due on {due:%d/%m/%Y} ({weekly['days_overdue']} day(s) overdue)."
                    elif weekly["anc_status"] == "due_soon":
                        event, title = "anc_visit_due", "Weekly ANC visit due"
                        msg = f"{pw.full_name}: weekly ANC visit is due on {due:%d/%m/%Y}."
                elif visit and visit.next_visit_date:
                    # Below 28 weeks: regular ANC
                    days_left = (visit.next_visit_date - today).days
                    if days_left < 0:
                        event, title = "anc_overdue", "ANC visit overdue"
                        msg = f"{pw.full_name}: ANC visit was due on {visit.next_visit_date:%d/%m/%Y} ({abs(days_left)} day(s) overdue)."
                    elif days_left <= ANC_DUE_WINDOW_DAYS:
                        event, title = "anc_due", "ANC visit due"
                        msg = f"{pw.full_name}: ANC visit is due on {visit.next_visit_date:%d/%m/%Y}."

                if not event:
                    continue
                created = raise_alert(
                    db, event, pw,
                    title=title, message=msg,
                    alert_key=f"{event}:{pw.id}",
                    reference_id=pw.id,
                    reference_type="pregnant_woman",
                    action_url=f"/pregnant-women/{pw.id}",
                    metadata={"pregnant_woman_name": pw.full_name},
                )
                counts[event] = counts.get(event, 0) + (1 if created else 0)

                # ANC due -> SMS to the beneficiary too (only the first time the alert is raised)
                if event == "anc_due" and created and visit and visit.next_visit_date:
                    from services.alert_sms import send_template_sms_sync
                    send_template_sms_sync(
                        "anc_reminder", pw.mobile_number, pw.id,
                        DATE=visit.next_visit_date.strftime("%d/%m/%Y"),
                    )

            # PMSMA due: scheduled / rescheduled session in the next 2 days
            start = datetime.combine(today, datetime.min.time())
            end = datetime.combine(today + timedelta(days=PMSMA_DUE_WINDOW_DAYS), datetime.max.time())
            sessions = db.query(PMSMASession).filter(
                PMSMASession.status.in_(["scheduled", "rescheduled"]),
                PMSMASession.scheduled_date >= start,
                PMSMASession.scheduled_date <= end,
            ).all()
            for s in sessions:
                pw = pw_map.get(s.pregnant_woman_id)
                if not pw:
                    continue
                created = raise_alert(
                    db, "pmsma_due", pw,
                    title="PMSMA session due",
                    message=f"{pw.full_name}: PMSMA session on {s.scheduled_date:%d/%m/%Y %I:%M %p}.",
                    alert_key=f"pmsma_due:{s.id}",
                    reference_id=s.id,
                    reference_type="pmsma_session",
                    action_url="/pmsma-sessions",
                    metadata={"pregnant_woman_name": pw.full_name},
                )
                counts["pmsma_due"] = counts.get("pmsma_due", 0) + (1 if created else 0)

            logger.info(f"[Scheduler] ANC/PMSMA alerts DONE — {counts}")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Scheduler] ANC/PMSMA alerts FAILED — {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# Notification & Escalation module (spec M) — extra jobs
# ─────────────────────────────────────────────────────────────────────────────

REFERRAL_PENDING_AFTER_HOURS = int(os.getenv("REFERRAL_PENDING_AFTER_HOURS", "2"))
_ANC_ALERT_TYPES = ("anc_due", "anc_overdue", "anc_visit_due", "anc_visit_missed")


def job_referral_pending_alerts():
    """
    Job: referral still 'pending' (not accepted by the DP) after REFERRAL_PENDING_AFTER_HOURS
    -> referral_pending alert to concerned DP + Block + District (alert matrix, spec M).
    Also closes referral_pending alerts whose referral is no longer pending.
    Runs every 30 minutes; raise_alert() dedupes via alert_key so nothing repeats.
    """
    logger.info(f"[Scheduler] Referral pending alerts started at {datetime.now()}")
    try:
        from database import SessionLocal
        from models import DeliveryReferral, DeliveryPoint, PregnantWoman, Notification
        from services.alert_rules import raise_alert, close_alerts

        db = SessionLocal()
        try:
            cutoff = datetime.now() - timedelta(hours=REFERRAL_PENDING_AFTER_HOURS)
            raised = closed = 0

            pending = db.query(DeliveryReferral).filter(
                DeliveryReferral.status == "pending",
                DeliveryReferral.created_at <= cutoff,
            ).all()
            for r in pending:
                pw = db.query(PregnantWoman).filter(PregnantWoman.id == r.pregnant_woman_id).first()
                dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == r.dp_id).first()
                if not pw:
                    continue
                created = raise_alert(
                    db, "referral_pending", pw,
                    title="Referral pending",
                    message=(f"{pw.full_name}: referral to {dp.name if dp else 'the Delivery Point'} "
                             f"is still pending since {r.created_at:%d/%m/%Y %I:%M %p}."),
                    alert_key=f"referral_pending:{r.id}",
                    reference_id=r.id,
                    reference_type="delivery_referral",
                    action_url=f"/delivery-referrals/{r.id}",
                    context={"dp_id": r.dp_id,
                             "block_id": dp.block_id if dp else None,
                             "district_id": dp.district_id if dp else None},
                    metadata={"pregnant_woman_name": pw.full_name},
                )
                raised += 1 if created else 0

            # closure sweep: referral accepted / re-referred / completed meanwhile
            open_keys = db.query(Notification.alert_key).filter(
                Notification.notification_type == "referral_pending",
                Notification.is_resolved == False,
            ).distinct().all()
            for (key,) in open_keys:
                try:
                    rid = int(key.split(":")[1])
                except Exception:
                    continue
                r = db.query(DeliveryReferral).filter(DeliveryReferral.id == rid).first()
                if not r or r.status != "pending":
                    closed += close_alerts(db, key, note="Referral no longer pending")

            logger.info(f"[Scheduler] Referral pending alerts DONE — raised={raised}, closed={closed}")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Scheduler] Referral pending alerts FAILED — {str(e)}")


def job_escalate_alerts():
    """
    Job: three-level escalation (responsible user -> Block -> District).
      1. Close ANC alerts of women who delivered / were deactivated (nothing left to do).
      2. Close pmsma_due alerts whose session is no longer scheduled/rescheduled.
      3. Escalate every unresolved alert older than its limit (services/alert_rules.ESCALATION_HOURS).
    Each step is written to escalation_logs (action = closed / escalated). Runs hourly.
    """
    logger.info(f"[Scheduler] Alert escalation started at {datetime.now()}")
    try:
        from database import SessionLocal
        from models import Notification, PregnantWoman, PMSMASession
        from services.alert_rules import close_alerts, escalate_unresolved_alerts

        db = SessionLocal()
        try:
            closed = 0

            anc_keys = db.query(Notification.alert_key).filter(
                Notification.notification_type.in_(_ANC_ALERT_TYPES),
                Notification.is_resolved == False,
                Notification.alert_key.isnot(None),
            ).distinct().all()
            for (key,) in anc_keys:
                try:
                    pw_id = int(key.split(":")[1])
                except Exception:
                    continue
                pw = db.query(PregnantWoman).filter(PregnantWoman.id == pw_id).first()
                if not pw or not pw.is_active or pw.pregnancy_outcome:
                    closed += close_alerts(db, key, note="Pregnancy no longer active")

            pm_keys = db.query(Notification.alert_key).filter(
                Notification.notification_type == "pmsma_due",
                Notification.is_resolved == False,
                Notification.alert_key.isnot(None),
            ).distinct().all()
            for (key,) in pm_keys:
                try:
                    sid = int(key.split(":")[1])
                except Exception:
                    continue
                s = db.query(PMSMASession).filter(PMSMASession.id == sid).first()
                if not s or s.status not in ("scheduled", "rescheduled"):
                    closed += close_alerts(db, key, note="PMSMA session no longer due")

            counts = escalate_unresolved_alerts(db)
            logger.info(f"[Scheduler] Alert escalation DONE — closed={closed}, escalated={counts}")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Scheduler] Alert escalation FAILED — {str(e)}")

def job_daily_dashboard_snapshot():
    """Job: store today's District/Block dashboard KPI numbers. Runs daily just after midnight."""
    logger.info(f"[Scheduler] Dashboard snapshot started at {datetime.now()}")
    try:
        from database import SessionLocal
        from services.dashboard_snapshot_service import run_daily_snapshot

        db = SessionLocal()
        try:
            rows = run_daily_snapshot(db)
            logger.info(f"[Scheduler] Dashboard snapshot DONE — {rows} row(s)")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Scheduler] Dashboard snapshot FAILED — {str(e)}")



def start_scheduler():
    """
    Start APScheduler with all registered jobs.
    Called during FastAPI app startup (lifespan).
    """
    # Job 1: High-risk advisory IVR calls — daily at 9 AM
    scheduler.add_job(
        func=job_high_risk_advisory_calls,
        trigger=CronTrigger(hour=9, minute=0),
        id="high_risk_advisory_calls",
        name="High Risk Advisory IVR Calls",
        replace_existing=True,
        misfire_grace_time=3600
    )

    # Job 2: Feedback Call 1 — every 2 hours during business hours (10 AM to 5 PM)
    scheduler.add_job(
        func=job_feedback_call_1,
        trigger=CronTrigger(minute=0, hour="10,12,14,16"),  # Only at 10 AM, 12 PM, 2 PM, 4 PM
        id="feedback_call_1",
        name="Feedback Call 1 (10 AM to 4 PM only)",
        replace_existing=True,
        misfire_grace_time=1800  # 30 minutes grace
    )

    # Job 3: Feedback Call 2 — every 2 hours during business hours (10 AM to 5 PM)
    scheduler.add_job(
        func=job_feedback_call_2,
        trigger=CronTrigger(minute=30, hour="10,12,14,16"),  # Only at 10:30 AM, 12:30 PM, 2:30 PM, 4:30 PM
        id="feedback_call_2",
        name="Feedback Call 2 (10:30 AM to 4:30 PM only)",
        replace_existing=True,
        misfire_grace_time=1800  # 30 minutes grace
    )

    # Job 4: Generate/refresh mobilisation cases — daily at 6 AM
    scheduler.add_job(
        func=job_generate_mobilisation_cases,
        trigger=CronTrigger(hour=6, minute=0),
        id="generate_mobilisation_cases",
        name="Generate Mobilisation Cases",
        replace_existing=True,
        misfire_grace_time=3600
    )
        # Job 4b: EDD action engine — daily at 6:15 AM
    scheduler.add_job(
        func=job_generate_edd_action_cases,
        trigger=CronTrigger(hour=6, minute=15),
        id="generate_edd_action_cases",
        name="Generate EDD Action Cases",
        replace_existing=True,
        misfire_grace_time=3600
    )
    # Job 5: Escalate stale mobilisation cases — every 4 hours
    scheduler.add_job(
        func=job_escalate_mobilisation_cases,
        trigger=CronTrigger(minute=0, hour="*/4"),
        id="escalate_mobilisation_cases",
        name="Escalate Mobilisation Cases",
        replace_existing=True,
        misfire_grace_time=1800
    )

    # Job 6: Surface due PNC reminders — daily at 6:30 AM
    scheduler.add_job(
        func=job_surface_pnc_reminders,
        trigger=CronTrigger(hour=6, minute=30),
        id="surface_pnc_reminders",
        name="Surface PNC Reminders",
        replace_existing=True,
        misfire_grace_time=3600
    )
        # Job 7: ANC / PMSMA time-based alerts (alert matrix) — daily at 7 AM
    scheduler.add_job(
        func=job_generate_anc_pmsma_alerts,
        trigger=CronTrigger(hour=7, minute=0),
        id="generate_anc_pmsma_alerts",
        name="ANC / PMSMA Alerts",
        replace_existing=True,
        misfire_grace_time=3600
    )

    # Job 8: Referral pending alerts (DP + Block + District) — every 30 minutes
    scheduler.add_job(
        func=job_referral_pending_alerts,
        trigger=CronTrigger(minute="*/30"),
        id="referral_pending_alerts",
        name="Referral Pending Alerts",
        replace_existing=True,
        misfire_grace_time=900
    )

    # Job 9: Three-level alert escalation + closure sweep — hourly at :10
    scheduler.add_job(
        func=job_escalate_alerts,
        trigger=CronTrigger(minute=10),
        id="escalate_alerts",
        name="Alert Escalation (Sub-centre → Block → District)",
        replace_existing=True,
        misfire_grace_time=1800
    )
    # Job 10: Dashboard KPI snapshot — daily at 00:10
    scheduler.add_job(
        func=job_daily_dashboard_snapshot,
        trigger=CronTrigger(hour=0, minute=10),
        id="daily_dashboard_snapshot",
        name="Dashboard KPI Snapshot (daily)",
        replace_existing=True,
        misfire_grace_time=3600
    )
    scheduler.start()
    logger.info("[Scheduler] APScheduler started successfully")
    logger.info("[Scheduler] Jobs registered:")
    for job in scheduler.get_jobs():
        logger.info(f"  → {job.name} | Next run: {job.next_run_time}")


def stop_scheduler():
    """
    Stop APScheduler gracefully.
    Called during FastAPI app shutdown (lifespan).
    """
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[Scheduler] APScheduler stopped")
