"""
PNC Reminder Routes
Tracks postnatal care visit reminders (48hr / day 7 / day 42 by default) that
are auto-created on Discharge (see routes/delivery_referral_routes.py::discharge_case)
and surfaced by the scheduler as they come due (see
scheduler.py::job_surface_pnc_reminders). This module only exposes the
read/list view and the "mark done" action — no call/SMS/AI-call actions are
included by design, matching the Mobilisation feature pattern.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session, joinedload
from typing import Optional
from datetime import datetime
from pydantic import BaseModel

from database import get_db
from models import PNCReminder, User
from auth import get_current_active_user
from audit_utils import log_audit, get_client_info

router = APIRouter(prefix="/pnc-reminders", tags=["PNC Reminders"])


class MarkPNCDoneRequest(BaseModel):
    remarks: Optional[str] = None


def _scope_query_to_user(query, current_user: User, db: Session):
    """Restrict the reminder list to what this role is allowed to see."""
    if current_user.role == "sub_centre":
        query = query.filter(PNCReminder.sub_centre_id == current_user.sub_centre_id)
    elif current_user.role == "block":
        from models import PregnantWoman
        pw_ids = db.query(PregnantWoman.id).filter(PregnantWoman.block_id == current_user.block_id).subquery()
        query = query.filter(PNCReminder.pregnant_woman_id.in_(pw_ids))
    elif current_user.role == "district":
        from models import PregnantWoman
        pw_ids = db.query(PregnantWoman.id).filter(PregnantWoman.district_id == current_user.district_id).subquery()
        query = query.filter(PNCReminder.pregnant_woman_id.in_(pw_ids))
    else:
        query = query.filter(PNCReminder.id.is_(None))  # empty result for other roles
    return query


def _serialize_reminder(reminder: PNCReminder) -> dict:
    pw = reminder.pregnant_woman
    return {
        "id": reminder.id,
        "discharge_id": reminder.discharge_id,
        "pregnant_woman_id": reminder.pregnant_woman_id,
        "pregnant_woman_name": pw.full_name if pw else None,
        "mobile_number": pw.mobile_number if pw else None,
        "is_high_risk": pw.is_high_risk if pw else None,
        "sub_centre_id": reminder.sub_centre_id,
        "visit_label": reminder.visit_label,
        "due_date": reminder.due_date,
        "status": reminder.status,
        "completed_by": reminder.completed_by,
        "completed_by_name": reminder.completed_by_user.full_name if reminder.completed_by_user else None,
        "completed_at": reminder.completed_at,
        "remarks": reminder.remarks,
        "created_at": reminder.created_at,
        "updated_at": reminder.updated_at,
    }


@router.get("/")
async def get_pnc_reminders(
    status_filter: Optional[str] = None,
    visit_label: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    List PNC reminders scoped to the caller's role.
    Default (no status_filter): open reminders only (scheduled + due).
    """
    query = db.query(PNCReminder).options(
        joinedload(PNCReminder.pregnant_woman),
        joinedload(PNCReminder.completed_by_user),
    )
    query = _scope_query_to_user(query, current_user, db)

    if status_filter:
        query = query.filter(PNCReminder.status == status_filter)
    else:
        query = query.filter(PNCReminder.status.in_(["scheduled", "due"]))

    if visit_label:
        query = query.filter(PNCReminder.visit_label == visit_label)

    reminders = query.order_by(
        PNCReminder.status.desc(),  # "due" sorts before "scheduled"
        PNCReminder.due_date.asc(),
    ).all()

    return [_serialize_reminder(r) for r in reminders]


@router.get("/count")
async def get_pnc_reminder_count(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Quick count for dashboard 'PNC reminders due' KPI cards. Counts only 'due' reminders."""
    query = db.query(PNCReminder).filter(PNCReminder.status == "due")
    query = _scope_query_to_user(query, current_user, db)
    return {"count": query.count()}


@router.get("/{reminder_id}")
async def get_pnc_reminder(
    reminder_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    reminder = db.query(PNCReminder).options(
        joinedload(PNCReminder.pregnant_woman),
        joinedload(PNCReminder.completed_by_user),
    ).filter(PNCReminder.id == reminder_id).first()

    if not reminder:
        raise HTTPException(status_code=404, detail="PNC reminder not found")

    return _serialize_reminder(reminder)


@router.post("/{reminder_id}/mark-done")
async def mark_pnc_done(
    reminder_id: int,
    body: MarkPNCDoneRequest,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Mark a PNC reminder as done. Anyone in scope (ANM/Block/District) can
    close a reminder they can see — there's no call/SMS/AI-call step here,
    this simply records that the PNC visit happened.
    """
    reminder = db.query(PNCReminder).filter(PNCReminder.id == reminder_id).first()
    if not reminder:
        raise HTTPException(status_code=404, detail="PNC reminder not found")

    if reminder.status == "completed":
        raise HTTPException(status_code=400, detail="Reminder is already marked as completed")

    old_values = {"status": reminder.status}

    reminder.status = "completed"
    reminder.completed_by = current_user.id
    reminder.completed_at = datetime.now()
    reminder.remarks = body.remarks

    db.commit()
    db.refresh(reminder)

    ip_address, user_agent = get_client_info(request)
    log_audit(
        db=db,
        user_id=current_user.id,
        action="PNC_REMINDER_MARKED_DONE",
        entity_type="PNCReminder",
        entity_id=reminder.id,
        old_values=old_values,
        new_values={"status": reminder.status, "remarks": reminder.remarks},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    return _serialize_reminder(reminder)
