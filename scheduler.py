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
                "To override, set is_emergency_override=true and provide override_reason."
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


def _upsert_mobilisation_case(db, pw, trigger_type, detail, reference_id=None):
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
        return

    db.add(MobilisationCase(
        pregnant_woman_id=pw.id,
        trigger_type=trigger_type,
        trigger_reference_id=reference_id,
        trigger_detail=detail,
        sub_centre_id=pw.sub_centre_id,
        block_id=pw.block_id,
        district_id=pw.district_id,
        status="pending",
        escalation_level="anm",
    ))


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
            near_edd_cutoff = today + timedelta(days=15)
            for pw in active_pws:
                if pw.edd_date and today <= pw.edd_date <= near_edd_cutoff:
                    days_left = (pw.edd_date - today).days
                    _upsert_mobilisation_case(
                        db, pw, "near_edd", f"EDD in {days_left} day(s)"
                    )

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
                        db, pw, "hrp_unreferred", "High-risk case not yet referred to a delivery point"
                    )

            # 6) Close cases whose trigger has resolved itself
            _auto_close_resolved_mobilisation_cases(db)

            db.commit()
            logger.info("[Scheduler] Mobilisation case generation DONE")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[Scheduler] Mobilisation case generation FAILED — {str(e)}")


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
