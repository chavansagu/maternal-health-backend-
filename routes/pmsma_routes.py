from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Optional
from datetime import datetime, date
from pydantic import BaseModel

from database import get_db
from models import PMSMASession, PregnantWoman, User, PMSMACentre
from auth import get_current_active_user
from audit_utils import get_client_info, get_entity_snapshot, log_create, log_update
from services.notification_service import NotificationService
from services.tracking_service import get_tracking_info

router = APIRouter(prefix="/pmsma-sessions", tags=["PMSMA Sessions"])

PMSMA_ROLE = "pmsma"


# ── Request body schemas ─────────────────────────────────────────────────────

class PMSMASessionCreate(BaseModel):
    pregnant_woman_id: int
    scheduled_date: datetime
    pmsma_centre_id: Optional[int] = None
    site: Optional[str] = None  # free-text fallback, used only if pmsma_centre_id is not set
    appointment_type: str = "regular"


class PMSMASessionComplete(BaseModel):
    bp: Optional[str] = None
    blood_sugar: Optional[float] = None
    hb: Optional[float] = None
    weight: Optional[float] = None
    additional_parameters: Optional[str] = None
    counselling_notes: Optional[str] = None
    is_high_risk: bool = False


class PMSMASessionReschedule(BaseModel):
    new_scheduled_date: datetime
    reschedule_reason: str
    is_emergency_override: bool = False
    override_reason: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pmsma_counterpart_recipients(db: Session, session: "PMSMASession", pw: "PregnantWoman", current_user: User) -> list:
    """Who should be told about a change to this PMSMA session, other than the
    person who just made the change.

    - If the actor is Sub-Centre/ANM, notify PMSMA users in the same block.
    - If the actor is PMSMA, notify Sub-Centre/ANM users at the woman's sub-centre.
    Always excludes the actor themselves.
    """
    recipients = []
    if current_user.role == "sub_centre":
        users = db.query(User).filter(
            User.role == PMSMA_ROLE,
            User.block_id == session.block_id,
            User.is_active == True,
        ).all()
        recipients = [u.id for u in users]
    else:
        users = db.query(User).filter(
            User.role == "sub_centre",
            User.sub_centre_id == pw.sub_centre_id,
            User.is_active == True,
        ).all()
        recipients = [u.id for u in users]

    return [uid for uid in set(recipients) if uid != current_user.id]


def _require_pmsma(current_user: User):
    if current_user.role != PMSMA_ROLE:
        raise HTTPException(status_code=403, detail="PMSMA user access required")


def _require_scheduler(current_user: User):
    """Sub-Centre/ANM schedules the PMSMA test; PMSMA users may also self-schedule."""
    if current_user.role not in ["sub_centre", PMSMA_ROLE]:
        raise HTTPException(status_code=403, detail="Sub-Centre or PMSMA user access required")


def _require_same_block(current_user: User, session: "PMSMASession"):
    """A session is visible/actionable to any user (ANM or PMSMA) in the same block
    it was scheduled for — not just the person who created it."""
    if current_user.block_id is None or session.block_id != current_user.block_id:
        raise HTTPException(status_code=403, detail="Not authorized for this session")


def _fmt(session: PMSMASession, db: Session) -> dict:
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == session.pregnant_woman_id).first()
    centre = None
    if session.pmsma_centre_id:
        centre = db.query(PMSMACentre).filter(PMSMACentre.id == session.pmsma_centre_id).first()
    tracking = get_tracking_info(pw)    
    return {
        "id": session.id,
        "pregnant_woman_id": session.pregnant_woman_id,
        "pregnant_woman_name": pw.full_name if pw else None,
        "scheduled_date": session.scheduled_date,
        "original_scheduled_date": session.original_scheduled_date,
        "block_id": session.block_id,
        "site": session.site,
        "pmsma_centre_id": session.pmsma_centre_id,
        "pmsma_centre_name": centre.name if centre else None,
        "pmsma_centre_code": centre.code if centre else None,
        "status": session.status,
        "appointment_type": session.appointment_type,
        "bp": session.bp,
        "blood_sugar": session.blood_sugar,
        "hb": session.hb,
        "weight": session.weight,
        "additional_parameters": session.additional_parameters,
        "counselling_notes": session.counselling_notes,
        # Effective status: the woman's live high-risk flag (set from ANC/USG/registration
        # or an earlier PMSMA session) OR the flag recorded on this session. Reading only
        # the session column showed False for every scheduled session of a high-risk woman.
        "is_high_risk": bool(session.is_high_risk) or bool(pw.is_high_risk if pw else False),
        "scheduled_by": session.scheduled_by,
        "completed_by": session.completed_by,
        "reschedule_reason": session.reschedule_reason,
        "is_emergency_override": session.is_emergency_override,
        "override_reason": session.override_reason,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "gestational_weeks": tracking["gestational_weeks"],
        "tracking_type": tracking["tracking_type"],
    }


# ── POST /pmsma-sessions/ ────────────────────────────────────────────────────

@router.post("/", status_code=status.HTTP_201_CREATED)
async def schedule_pmsma_session(
    body: PMSMASessionCreate,
    request: Request = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Schedule a PMSMA session for a pregnant woman (Sub-Centre/ANM or PMSMA)."""
    _require_scheduler(current_user)

    pw = db.query(PregnantWoman).filter(PregnantWoman.id == body.pregnant_woman_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")

    if current_user.role == "sub_centre" and pw.sub_centre_id != current_user.sub_centre_id:
        raise HTTPException(status_code=403, detail="Not authorized for this pregnant woman")

    if not pw.is_active or pw.pregnancy_outcome is not None:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot schedule PMSMA session. Beneficiary '{pw.full_name}' has already completed delivery.",
        )

    centre = None
    if body.pmsma_centre_id:
        centre = db.query(PMSMACentre).filter(PMSMACentre.id == body.pmsma_centre_id).first()
        if not centre or not centre.is_active:
            raise HTTPException(status_code=400, detail="Selected PMSMA centre not found or inactive")

    day_start = body.scheduled_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = body.scheduled_date.replace(hour=23, minute=59, second=59, microsecond=999999)
    existing = db.query(PMSMASession).filter(
        PMSMASession.pregnant_woman_id == body.pregnant_woman_id,
        PMSMASession.scheduled_date >= day_start,
        PMSMASession.scheduled_date <= day_end,
        PMSMASession.status.in_(["scheduled", "rescheduled"]),
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"An active PMSMA session already exists for {pw.full_name} on {body.scheduled_date.date()}.",
        )

    session = PMSMASession(
        pregnant_woman_id=body.pregnant_woman_id,
        scheduled_date=body.scheduled_date,
        original_scheduled_date=body.scheduled_date,
        block_id=current_user.block_id,
        site=body.site if not centre else centre.name,
        pmsma_centre_id=body.pmsma_centre_id,
        appointment_type=body.appointment_type,
        status="scheduled",
        is_high_risk=bool(pw.is_high_risk),
        scheduled_by=current_user.id,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    ip_address, user_agent = get_client_info(request) if request else (None, None)
    log_create(db, current_user.id, "PMSMASession", session.id, get_entity_snapshot(session), ip_address, user_agent)

    recipients = _pmsma_counterpart_recipients(db, session, pw, current_user)
    if recipients:
        NotificationService.create_notification(
            db=db,
            user_ids=recipients,
            title="New PMSMA Session Scheduled",
            message=f"A PMSMA session was scheduled for {pw.full_name} on {session.scheduled_date.strftime('%d-%m-%Y')}.",
            notification_type="pmsma_session_scheduled",
            category="pmsma",
            priority="normal",
            reference_id=session.id,
            reference_type="pmsma_session",
            action_url="/pmsma-scheduling",
            metadata={"pregnant_woman_name": pw.full_name},
        )

    return _fmt(session, db)


# ── GET /pmsma-sessions/queue ────────────────────────────────────────────────

@router.get("/queue")
async def get_pmsma_queue(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Return today's and upcoming PMSMA sessions for the logged-in PMSMA user.
    Scoped to the user's own PMSMA centre when their login is tied to one;
    falls back to their whole block if not (e.g. legacy users, or centre-less setups)."""
    _require_pmsma(current_user)

    if current_user.block_id is None:
        return []

    today_start = datetime.combine(date.today(), datetime.min.time())
    filters = [
        PMSMASession.block_id == current_user.block_id,
        PMSMASession.scheduled_date >= today_start,
        PMSMASession.status.in_(["scheduled", "rescheduled"]),
    ]
    if current_user.pmsma_centre_id:
        # Sessions booked at this user's centre, plus sessions scheduled with a
        # free-text site (no centre picked) — those still belong to this block's
        # queue and must not be silently dropped just because pmsma_centre_id is NULL.
        filters.append(
            or_(
                PMSMASession.pmsma_centre_id == current_user.pmsma_centre_id,
                PMSMASession.pmsma_centre_id.is_(None),
            )
        )

    sessions = (
        db.query(PMSMASession)
        .filter(*filters)
        .order_by(PMSMASession.scheduled_date)
        .all()
    )
    return [_fmt(s, db) for s in sessions]


# ── GET /pmsma-sessions/district-overview ───────────────────────────────────

@router.get("/district-overview")
async def get_district_pmsma_overview(
    status_filter: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Read-only, district-wide view of PMSMA sessions for District Admins.
    Scoped to pregnant women belonging to the admin's own district. Supports
    an optional status filter (e.g. 'scheduled', 'rescheduled', 'completed')
    plus the same date-range/search filters as the other listing endpoints.
    District Admins cannot complete or reschedule sessions from this view —
    that remains the responsibility of PMSMA/Sub-Centre users."""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="District Admin access required")

    if current_user.district_id is None:
        return []

    query = (
        db.query(PMSMASession)
        .join(PregnantWoman, PregnantWoman.id == PMSMASession.pregnant_woman_id)
        .filter(PregnantWoman.district_id == current_user.district_id)
    )

    if status_filter:
        query = query.filter(PMSMASession.status == status_filter)
    if date_from:
        query = query.filter(PMSMASession.scheduled_date >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(PMSMASession.scheduled_date <= datetime.combine(date_to, datetime.max.time()))

    sessions = query.order_by(PMSMASession.scheduled_date.desc()).all()
    results = [_fmt(s, db) for s in sessions]

    if search:
        term = search.lower()
        results = [r for r in results if term in (r.get("pregnant_woman_name") or "").lower() or term in str(r["id"])]

    return results


# ── GET /pmsma-sessions/dashboard-summary ────────────────────────────────────

@router.get("/dashboard-summary")
async def get_pmsma_dashboard_summary(
    block_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """District Dashboard KPI summary: counts of PMSMA appointments by status,
    scoped to the District Admin's own district. Mirrors the filter pattern
    used by /district-overview (optional block_id, date_from, date_to)."""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="District Admin access required")

    if current_user.district_id is None:
        return {"total_appointments": 0, "pending": 0, "completed": 0, "rescheduled": 0}

    query = (
        db.query(PMSMASession)
        .join(PregnantWoman, PregnantWoman.id == PMSMASession.pregnant_woman_id)
        .filter(PregnantWoman.district_id == current_user.district_id)
    )

    if block_id:
        query = query.filter(PMSMASession.block_id == block_id)
    if date_from:
        query = query.filter(PMSMASession.scheduled_date >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(PMSMASession.scheduled_date <= datetime.combine(date_to, datetime.max.time()))

    total_appointments = query.count()
    pending = query.filter(PMSMASession.status == "scheduled").count()
    completed = query.filter(PMSMASession.status == "completed").count()
    rescheduled = query.filter(PMSMASession.status == "rescheduled").count()

    return {
        "total_appointments": total_appointments,
        "pending": pending,
        "completed": completed,
        "rescheduled": rescheduled,
    }


# ── GET /pmsma-sessions/completed ────────────────────────────────────────────

@router.get("/completed")
async def get_pmsma_completed_sessions(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Return completed PMSMA sessions (with recorded test/counselling data) for the
    logged-in PMSMA user, most recent first. Scoped to the user's own PMSMA centre
    when set, else their whole block. Optional date range and name search."""
    _require_pmsma(current_user)

    if current_user.block_id is None:
        return []

    query = db.query(PMSMASession).filter(
        PMSMASession.block_id == current_user.block_id,
        PMSMASession.status == "completed",
    )
    if current_user.pmsma_centre_id:
        query = query.filter(PMSMASession.pmsma_centre_id == current_user.pmsma_centre_id)

    if date_from:
        query = query.filter(PMSMASession.scheduled_date >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(PMSMASession.scheduled_date <= datetime.combine(date_to, datetime.max.time()))

    sessions = query.order_by(PMSMASession.scheduled_date.desc()).all()
    results = [_fmt(s, db) for s in sessions]

    if search:
        term = search.lower()
        results = [r for r in results if term in (r.get("pregnant_woman_name") or "").lower() or term in str(r["id"])]

    return results


# ── GET /pmsma-sessions/sub-centre ───────────────────────────────────────────

@router.get("/sub-centre")
async def get_sub_centre_pmsma_sessions(
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Return PMSMA sessions for pregnant women in the logged-in ANM's own
    sub-centre — across all statuses, for tracking, scheduling and rescheduling.
    Sub-Centre/ANM role only."""
    if current_user.role != "sub_centre":
        raise HTTPException(status_code=403, detail="Sub-Centre/ANM access required")

    if current_user.sub_centre_id is None:
        return []

    query = (
        db.query(PMSMASession)
        .join(PregnantWoman, PregnantWoman.id == PMSMASession.pregnant_woman_id)
        .filter(PregnantWoman.sub_centre_id == current_user.sub_centre_id)
    )

    if status_filter:
        query = query.filter(PMSMASession.status == status_filter)

    sessions = query.order_by(PMSMASession.scheduled_date.desc()).all()
    results = [_fmt(s, db) for s in sessions]

    if search:
        term = search.lower()
        results = [r for r in results if term in (r.get("pregnant_woman_name") or "").lower() or term in str(r["id"])]

    return results


# ── GET /pmsma-sessions/pregnant-woman/{pw_id} ──────────────────────────────

@router.get("/pregnant-woman/{pw_id}")
async def get_pmsma_sessions_for_pregnant_woman(
    pw_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get all PMSMA sessions for a specific pregnant woman."""
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pw_id).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")

    # Authorization check — matches GET /anc-visits/pregnant-woman/{pw_id}
    if current_user.role == "district" and pw.district_id != current_user.district_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "block" and pw.block_id != current_user.block_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "sub_centre":
        if pw.sub_centre_id is not None and pw.sub_centre_id != current_user.sub_centre_id:
            if pw.block_id != current_user.block_id:
                raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == PMSMA_ROLE:
        if pw.block_id != current_user.block_id:
            raise HTTPException(status_code=403, detail="Not authorized")

    sessions = (
        db.query(PMSMASession)
        .filter(PMSMASession.pregnant_woman_id == pw_id)
        .order_by(PMSMASession.scheduled_date)
        .all()
    )

    next_session_date = None
    upcoming = [s for s in sessions if s.status in ("scheduled", "rescheduled") and s.scheduled_date]
    if upcoming:
        next_session_date = min(s.scheduled_date for s in upcoming)

    return {
        "pregnant_woman": {
            "id": pw.id,
            "full_name": pw.full_name,
            "mobile_number": pw.mobile_number,
            "edd_date": pw.edd_date,
            "is_high_risk": pw.is_high_risk
        },
        "sessions": [_fmt(s, db) for s in sessions],
        "total_sessions": len(sessions),
        "next_session_date": next_session_date
    }


# ── PUT /pmsma-sessions/{id}/complete ────────────────────────────────────────

@router.put("/{session_id}/complete")
@router.post("/{session_id}/complete")
async def complete_pmsma_session(
    session_id: int,
    body: PMSMASessionComplete,
    request: Request = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Record test results and counselling notes for a PMSMA session (PMSMA role only)."""
    _require_pmsma(current_user)

    session = db.query(PMSMASession).filter(PMSMASession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="PMSMA session not found")

    _require_same_block(current_user, session)

    if session.status == "completed":
        raise HTTPException(status_code=400, detail="Session already completed")

    if session.status == "cancelled":
        raise HTTPException(status_code=400, detail="Cannot complete a cancelled session")

    old_values = get_entity_snapshot(session)
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == session.pregnant_woman_id).first()
    already_high_risk = bool(pw.is_high_risk) if pw else False
    session.status = "completed"
    session.bp = body.bp
    session.blood_sugar = body.blood_sugar
    session.hb = body.hb
    session.weight = body.weight
    session.additional_parameters = body.additional_parameters
    session.counselling_notes = body.counselling_notes
    session.is_high_risk = bool(body.is_high_risk) or already_high_risk
    session.completed_by = current_user.id
    session.updated_at = datetime.now()
    db.commit()
    db.refresh(session)

    if pw:
        recipients = _pmsma_counterpart_recipients(db, session, pw, current_user)
        if recipients:
            NotificationService.create_notification(
                db=db,
                user_ids=recipients,
                title="PMSMA Session Completed",
                message=f"PMSMA session for {pw.full_name} was completed and results recorded.",
                notification_type="pmsma_session_completed",
                category="pmsma",
                priority="normal",
                reference_id=session.id,
                reference_type="pmsma_session",
                action_url="/pmsma-session-management",
                metadata={"pregnant_woman_name": pw.full_name},
            )

    if body.is_high_risk:
        if pw and not pw.is_high_risk:
            pw.is_high_risk = True
            pw.risk_factors = (pw.risk_factors or "") + "; PMSMA session flagged high-risk"
            db.commit()

            recipients = NotificationService.get_recipients_for_event(
                db, "high_risk_alert",
                {"district_id": pw.district_id, "block_id": pw.block_id, "sub_centre_id": pw.sub_centre_id},
            )
            if recipients:
                NotificationService.create_notification(
                    db=db,
                    user_ids=recipients,
                    title="⚠️ High-Risk Case — PMSMA",
                    message=f"{pw.full_name} flagged high-risk during PMSMA session.",
                    notification_type="high_risk_alert",
                    category="pmsma",
                    priority="high",
                    reference_id=pw.id,
                    reference_type="pregnant_woman",
                    action_url=f"/pregnant-women/{pw.id}",
                    metadata={"pregnant_woman_name": pw.full_name},
                )

    ip_address, user_agent = get_client_info(request) if request else (None, None)
    log_update(db, current_user.id, "PMSMASession", session.id, old_values, get_entity_snapshot(session), ip_address, user_agent)

    return _fmt(session, db)


# ── POST /pmsma-sessions/{id}/reschedule ─────────────────────────────────────

@router.post("/{session_id}/reschedule")
async def reschedule_pmsma_session(
    session_id: int,
    body: PMSMASessionReschedule,
    request: Request = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Reschedule a PMSMA session (7-day window, or emergency override for PMSMA users).
    Sub-Centre/ANM and PMSMA can both reschedule; only PMSMA may use the emergency override."""
    _require_scheduler(current_user)

    if body.is_emergency_override and current_user.role != PMSMA_ROLE:
        raise HTTPException(
            status_code=403,
            detail="Only PMSMA users can use the emergency override for rescheduling",
        )

    session = db.query(PMSMASession).filter(PMSMASession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="PMSMA session not found")

    _require_same_block(current_user, session)

    if session.status in ["completed", "cancelled"]:
        raise HTTPException(status_code=400, detail=f"Cannot reschedule a {session.status} session")

    from scheduler import check_reschedule_window
    window = check_reschedule_window(
        body.new_scheduled_date,
        session.original_scheduled_date,
        body.is_emergency_override,
        body.override_reason,
    )

    old_values = get_entity_snapshot(session)
    session.scheduled_date = body.new_scheduled_date
    session.status = "rescheduled"
    session.reschedule_reason = body.reschedule_reason
    session.is_emergency_override = window["is_override"]
    session.override_reason = window["reason"]
    session.updated_at = datetime.now()
    db.commit()
    db.refresh(session)

    ip_address, user_agent = get_client_info(request) if request else (None, None)
    action = "EMERGENCY_OVERRIDE_RESCHEDULE" if window["is_override"] else "RESCHEDULE"
    log_update(
        db, current_user.id, "PMSMASession", session.id,
        old_values,
        {**get_entity_snapshot(session), "_action": action, "_override_reason": window["reason"]},
        ip_address, user_agent,
    )

    pw = db.query(PregnantWoman).filter(PregnantWoman.id == session.pregnant_woman_id).first()
    if pw:
        recipients = _pmsma_counterpart_recipients(db, session, pw, current_user)
        if recipients:
            is_override = window["is_override"]
            NotificationService.create_notification(
                db=db,
                user_ids=recipients,
                title="🚨 PMSMA Session Rescheduled (Emergency)" if is_override else "PMSMA Session Rescheduled",
                message=(
                    f"PMSMA session for {pw.full_name} was rescheduled to "
                    f"{session.scheduled_date.strftime('%d-%m-%Y %H:%M')}. Reason: {body.reschedule_reason}"
                ),
                notification_type="pmsma_session_rescheduled",
                category="pmsma",
                priority="high" if is_override else "normal",
                reference_id=session.id,
                reference_type="pmsma_session",
                action_url="/pmsma-scheduling",
                metadata={"pregnant_woman_name": pw.full_name, "is_emergency_override": is_override},
            )

    return _fmt(session, db)