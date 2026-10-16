"""
Alert Rules (M module)
One place that knows: who gets which alert (notification matrix),
how alerts are raised (deduped + logged) and how they are closed.
"""
import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional, Any

from sqlalchemy.orm import Session

from models import Notification, EscalationLog, User, UserWardMapping, PregnantWoman

logger = logging.getLogger(__name__)

# Escalation chain used by the escalation job (Step 3)
ESCALATION_CHAIN = ["sub_centre", "block", "district"]

# Prefix added to the title of an escalated notification (also used by dashboard summary)
ESCALATED_PREFIX = "[Escalated] "

# Hours an alert may stay unresolved before it is pushed to the next level.
# Defaults can be overridden from .env; per-event overrides below.
_BLOCK_H = int(os.getenv("ESCALATION_BLOCK_AFTER_HOURS", "24"))
_DISTRICT_H = int(os.getenv("ESCALATION_DISTRICT_AFTER_HOURS", "48"))
ESCALATION_HOURS: Dict[str, Dict[str, int]] = {
    "default":              {"block": _BLOCK_H, "district": _DISTRICT_H},
    "anc_visit_missed":     {"block": 24, "district": 72},
    "mobilisation_pending": {"district": 144},   # Block already notified at creation; 3d + 3d like the case chain
    "referral_pending":     {"block": 4, "district": 12},
}

# Notification matrix from spec M.
# roles         = who receives it (in-app)
# priority      = notification priority (default for every role)
# role_priority = optional per-role override, e.g. {"block": "high"}
# escalate      = True -> escalation job moves it up the chain if still unresolved
ALERT_MATRIX: Dict[str, Dict[str, Any]] = {
    "anc_due":                 {"roles": ["sub_centre"],                      "priority": "normal",   "escalate": False},
    "anc_overdue":             {"roles": ["sub_centre", "block"],             "priority": "high",     "escalate": False},
    "anc_visit_due":           {"roles": ["sub_centre"],                      "priority": "normal",   "escalate": False},
    "anc_visit_missed":        {"roles": ["sub_centre"],                      "priority": "high",     "escalate": True},
    "hrp_identified":          {"roles": ["sub_centre", "block", "district"], "priority": "high",     "escalate": False},
    "pmsma_due":               {"roles": ["sub_centre", "pmsma"],             "priority": "normal",   "escalate": False},
    "usg_scheduled":           {"roles": ["usg_centre", "pmsma", "sub_centre"], "priority": "normal", "escalate": False},
    "usg_high_risk_finding":   {"roles": ["sub_centre", "block", "district"], "priority": "high",     "escalate": False},
    # Near EDD: reminder to Sub-centre user only (spec I)
    "edd_approaching":         {"roles": ["sub_centre"],                      "priority": "normal",   "escalate": False},
    # HRP + near EDD: immediate (urgent) alert to Sub-centre; high priority for Block and District (spec I)
    "hrp_near_edd":            {"roles": ["sub_centre", "block", "district"], "priority": "urgent",   "escalate": True,
                                "role_priority": {"block": "high", "district": "high"}},
    "edd_today_not_admitted":  {"roles": ["sub_centre", "block", "district"], "priority": "urgent",   "escalate": True},
    "edd_crossed_no_delivery": {"roles": ["sub_centre", "block", "district"], "priority": "urgent",   "escalate": True},
    "mobilisation_pending":    {"roles": ["sub_centre", "block"],             "priority": "high",     "escalate": True},
    "referral_initiated":      {"roles": ["dp", "block", "district"],         "priority": "high",     "escalate": False},
    "referral_pending":        {"roles": ["dp", "block", "district"],         "priority": "high",     "escalate": True},
    "admission_confirmed":     {"roles": ["sub_centre", "block", "district"], "priority": "normal",   "escalate": False},
    "death_recorded":          {"roles": ["block", "district"],               "priority": "urgent",   "escalate": False},
}


# ─── Recipient resolution ───────────────────────────────────────────────────

def _users_for_role(db: Session, role: str, pw=None, context: Optional[dict] = None) -> List[int]:
    context = context or {}
    ids: List[int] = []

    if role == "sub_centre":
        if pw is not None and pw.ward_id:
            rows = db.query(UserWardMapping.user_id).filter(UserWardMapping.ward_id == pw.ward_id).all()
            ids = [r[0] for r in rows]
        sc_id = (pw.sub_centre_id if pw is not None else None) or context.get("sub_centre_id")
        if not ids and sc_id:
            ids = [u.id for u in db.query(User).filter(
                User.sub_centre_id == sc_id, User.role == "sub_centre", User.is_active == True).all()]

    elif role == "block":
        block_id = (pw.block_id if pw is not None else None) or context.get("block_id")
        if block_id:
            ids = [u.id for u in db.query(User).filter(
                User.block_id == block_id, User.role == "block", User.is_active == True).all()]

    elif role == "district":
        district_id = (pw.district_id if pw is not None else None) or context.get("district_id")
        if district_id:
            ids = [u.id for u in db.query(User).filter(
                User.district_id == district_id, User.role == "district", User.is_active == True).all()]

    elif role == "pmsma":
        block_id = (pw.block_id if pw is not None else None) or context.get("block_id")
        if block_id:
            ids = [u.id for u in db.query(User).filter(
                User.block_id == block_id, User.role == "pmsma", User.is_active == True).all()]

    elif role == "usg_centre":
        if context.get("usg_centre_id"):
            ids = [u.id for u in db.query(User).filter(
                User.usg_centre_id == context["usg_centre_id"], User.is_active == True).all()]

    elif role == "dp":
        if context.get("dp_id"):
            ids = [u.id for u in db.query(User).filter(
                User.dp_id == context["dp_id"], User.role == "dp", User.is_active == True).all()]

    return ids


# ─── Logging ────────────────────────────────────────────────────────────────

def log_escalation(db: Session, *, event_type: str, level: str, action: str,
                   user_id: Optional[int] = None, alert_key: Optional[str] = None,
                   pregnant_woman_id: Optional[int] = None, reference_type: Optional[str] = None,
                   reference_id: Optional[int] = None, note: Optional[str] = None) -> None:
    """Add a row to escalation_logs (caller commits)."""
    db.add(EscalationLog(
        alert_key=alert_key, event_type=event_type, pregnant_woman_id=pregnant_woman_id,
        reference_type=reference_type, reference_id=reference_id, level=level,
        user_id=user_id, action=action, action_note=note,
    ))


# ─── Raise / close ──────────────────────────────────────────────────────────

def raise_alert(
    db: Session,
    event_type: str,
    pw=None,
    *,
    title: str,
    message: str,
    alert_key: Optional[str] = None,
    reference_id: Optional[int] = None,
    reference_type: Optional[str] = None,
    action_url: Optional[str] = None,
    context: Optional[dict] = None,
    metadata: Optional[dict] = None,
    only_roles: Optional[List[str]] = None,
    priority: Optional[str] = None,
    log_action: str = "notified",
    log_note: Optional[str] = None,
) -> List[Notification]:
    """
    Raise an alert per the notification matrix.
    - Recipients come from ALERT_MATRIX[event_type] (or only_roles when escalating).
    - Priority: explicit `priority` arg > matrix `role_priority` for that role > matrix default.
    - If alert_key is given, a user who already has an UNRESOLVED alert with that key is skipped.
    - Every notification is written to escalation_logs (action='notified').
    """
    rule = ALERT_MATRIX.get(event_type)
    if not rule:
        logger.warning(f"[Alerts] Unknown event_type: {event_type}")
        return []

    roles = only_roles or rule["roles"]
    role_prio = rule.get("role_priority", {})
    meta = json.dumps(metadata) if metadata else None
    created: List[Notification] = []
    seen = set()

    try:
        for role in roles:
            for uid in _users_for_role(db, role, pw, context):
                if uid in seen:
                    continue
                seen.add(uid)
                if alert_key:
                    exists = db.query(Notification.id).filter(
                        Notification.user_id == uid,
                        Notification.alert_key == alert_key,
                        Notification.is_resolved == False,
                        Notification.deleted_at.is_(None),
                    ).first()
                    if exists:
                        continue
                # explicit priority > per-role priority from the matrix > matrix default
                prio = priority or role_prio.get(role) or rule["priority"]
                n = Notification(
                    user_id=uid, title=title, message=message,
                    notification_type=event_type, category="alert", priority=prio,
                    reference_id=reference_id, reference_type=reference_type,
                    action_url=action_url, extra_data=meta,
                    alert_key=alert_key, escalation_level=role, is_resolved=False,
                )
                db.add(n)
                created.append(n)
                log_escalation(
                    db, event_type=event_type, level=role, action=log_action, user_id=uid,
                    alert_key=alert_key, pregnant_woman_id=pw.id if pw is not None else None,
                    reference_type=reference_type, reference_id=reference_id, note=log_note or title,
                )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[Alerts] raise_alert failed for {event_type}: {e}")
        return []

    # Push notification (best-effort, runs in a background thread, never blocks/raises)
    try:
        from services.push_service import dispatch_push
        dispatch_push([
            {
                "user_id": n.user_id, "title": n.title, "body": n.message,
                "data": {
                    "notification_id": n.id, "type": n.notification_type,
                    "priority": n.priority, "action_url": n.action_url or "",
                },
            }
            for n in created
        ])
    except Exception as e:
        logger.warning(f"[Alerts] push dispatch skipped: {e}")
    return created


def close_alerts(db: Session, alert_key: str, *, note: str = "Required entry completed",
                 commit: bool = True) -> int:
    """Mark all open notifications with this alert_key as resolved and log it.
    commit=False lets callers that are mid-transaction commit themselves."""
    now = datetime.now()
    rows = db.query(Notification).filter(
        Notification.alert_key == alert_key, Notification.is_resolved == False
    ).all()
    for n in rows:
        n.is_resolved = True
        n.resolved_at = now
        log_escalation(
            db, event_type=n.notification_type, level=n.escalation_level or "unknown",
            action="closed", user_id=n.user_id, alert_key=alert_key, note=note,
        )
    if rows and commit:
        db.commit()
    return len(rows)


def close_case_alerts(db: Session, case_ids, *, note: str = "Mobilisation case closed",
                      commit: bool = False) -> int:
    """Close the alert (alert_key = mobcase:<id>) of each mobilisation / EDD case."""
    total = 0
    for cid in case_ids:
        total += close_alerts(db, f"mobcase:{cid}", note=note, commit=False)
    if total and commit:
        db.commit()
    return total


# ─── Escalation (Responsible user -> Block -> District) ─────────────────────

def _pw_for_alert(db: Session, alert_key: str):
    """Pregnant woman behind an alert (taken from the first escalation-log row)."""
    row = db.query(EscalationLog.pregnant_woman_id).filter(
        EscalationLog.alert_key == alert_key,
        EscalationLog.pregnant_woman_id.isnot(None),
    ).first()
    if not row:
        return None
    return db.query(PregnantWoman).filter(PregnantWoman.id == row[0]).first()


def escalate_alert(db: Session, alert_key: str, to_level: str, *, pw=None,
                   note: Optional[str] = None) -> int:
    """
    Push one open alert up to `to_level` (block / district).
    Creates an urgent '[Escalated]' notification for that level's users and writes
    an escalation_logs row (action='escalated'). Idempotent: users who already have
    the alert open are skipped by raise_alert().
    Returns number of notifications created.
    """
    base = db.query(Notification).filter(
        Notification.alert_key == alert_key,
        Notification.is_resolved == False,
        Notification.deleted_at.is_(None),
    ).order_by(Notification.created_at.asc()).first()
    if not base:
        return 0
    if pw is None:
        pw = _pw_for_alert(db, alert_key)
    if pw is None:
        logger.warning(f"[Alerts] cannot escalate {alert_key}: pregnant woman unknown")
        return 0

    try:
        meta = json.loads(base.extra_data) if base.extra_data else {}
    except Exception:
        meta = {}
    meta.update({"escalated": True, "escalated_to": to_level})
    age_h = int(((datetime.now() - base.created_at).total_seconds() // 3600)) if base.created_at else 0
    title = base.title if base.title.startswith(ESCALATED_PREFIX) else ESCALATED_PREFIX + base.title

    created = raise_alert(
        db, base.notification_type, pw,
        title=title,
        message=f"{base.message} (unresolved for {age_h}h, escalated to {to_level})",
        alert_key=alert_key,
        reference_id=base.reference_id, reference_type=base.reference_type,
        action_url=base.action_url, metadata=meta,
        only_roles=[to_level], priority="urgent",
        log_action="escalated",
        log_note=note or f"Unresolved for {age_h}h - escalated to {to_level}",
    )
    return len(created)


def escalate_unresolved_alerts(db: Session) -> Dict[str, int]:
    """
    Escalation job body. For every unresolved alert whose event has escalate=True,
    notify Block, then District once the alert is older than the configured hours
    (ESCALATION_HOURS). Levels that were already notified at creation (per the
    notification matrix) are never repeated.
    """
    now = datetime.now()
    counts = {"block": 0, "district": 0}
    events = [k for k, v in ALERT_MATRIX.items() if v.get("escalate")]

    rows = db.query(Notification).filter(
        Notification.alert_key.isnot(None),
        Notification.is_resolved == False,
        Notification.deleted_at.is_(None),
        Notification.notification_type.in_(events),
    ).all()

    by_key: Dict[str, List[Notification]] = {}
    for n in rows:
        by_key.setdefault(n.alert_key, []).append(n)

    for key, items in by_key.items():
        first = min(items, key=lambda r: r.created_at or now)
        if not first.created_at:
            continue
        notified = {r.escalation_level for r in items if r.escalation_level}
        limits = {**ESCALATION_HOURS["default"], **ESCALATION_HOURS.get(first.notification_type, {})}
        age_h = (now - first.created_at).total_seconds() / 3600
        pw = None
        for level in ("block", "district"):
            if level in notified or age_h < limits.get(level, 10**9):
                continue
            if pw is None:
                pw = _pw_for_alert(db, key)
            counts[level] += escalate_alert(db, key, level, pw=pw)
    return counts