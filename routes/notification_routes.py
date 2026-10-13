import json
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, or_, func, case
from sqlalchemy.orm import Session
from typing import List, Optional

from database import get_db
from models import User, Notification, EscalationLog, PregnantWoman
from services import push_service
from services.alert_rules import ESCALATED_PREFIX
from schemas import NotificationResponse, NotificationStats
from auth import get_current_active_user
from services.notification_service import NotificationService

router = APIRouter(prefix="/notifications", tags=["Notifications"])

@router.get("/", response_model=List[NotificationResponse])
async def get_notifications(
    skip: int = 0,
    limit: int = 20,
    is_read: Optional[bool] = None,
    category: Optional[str] = None,
    priority: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get user's notifications with optional filters
    """
    notifications = NotificationService.get_notifications(
        db=db,
        user_id=current_user.id,
        skip=skip,
        limit=limit,
        is_read=is_read,
        category=category,
        priority=priority
    )
    
    # Parse metadata JSON for response
    result = []
    for notification in notifications:
        notif_dict = {
            "id": notification.id,
            "user_id": notification.user_id,
            "title": notification.title,
            "message": notification.message,
            "notification_type": notification.notification_type,
            "category": notification.category,
            "priority": notification.priority,
            "reference_id": notification.reference_id,
            "reference_type": notification.reference_type,
            "action_url": notification.action_url,
            "extra_data": None,
            "is_read": notification.is_read,
            "read_at": notification.read_at,
            "deleted_at": notification.deleted_at,
            "created_at": notification.created_at,
            "alert_key": notification.alert_key,
            "is_resolved": bool(notification.is_resolved),
            "resolved_at": notification.resolved_at,
            "escalation_level": notification.escalation_level,
        }
        
        # Parse extra_data if exists
        if notification.extra_data:
            import json
            try:
                notif_dict["extra_data"] = json.loads(notification.extra_data)
            except:
                notif_dict["extra_data"] = None
        
        result.append(notif_dict)
    
    return result

@router.get("/unread-count")
async def get_unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get count of unread notifications
    """
    count = NotificationService.get_unread_count(db, current_user.id)
    return {"unread_count": count}

@router.get("/statistics", response_model=NotificationStats)
async def get_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get notification statistics
    """
    stats = NotificationService.get_statistics(db, current_user.id)
    return stats


# ─── Helpers ────────────────────────────────────────────────────────────────

def _notif_dict(n: Notification) -> dict:
    extra = None
    if n.extra_data:
        try:
            extra = json.loads(n.extra_data)
        except Exception:
            extra = None
    return {
        "id": n.id, "user_id": n.user_id, "title": n.title, "message": n.message,
        "notification_type": n.notification_type, "category": n.category, "priority": n.priority,
        "reference_id": n.reference_id, "reference_type": n.reference_type,
        "action_url": n.action_url, "extra_data": extra,
        "is_read": n.is_read, "read_at": n.read_at, "deleted_at": n.deleted_at,
        "created_at": n.created_at,
        "alert_key": n.alert_key, "is_resolved": bool(n.is_resolved),
        "resolved_at": n.resolved_at, "escalation_level": n.escalation_level,
    }


class SyncAckRequest(BaseModel):
    ids: List[int]


class DeviceRegisterRequest(BaseModel):
    token: str
    platform: str = "web"   # web / android / ios


class DeviceUnregisterRequest(BaseModel):
    token: str


# ─── Offline sync (spec M: pending alerts delivered on next synchronisation) ─

@router.get("/sync")
async def sync_notifications(
    since: Optional[datetime] = Query(None, description="Last successful sync time (ISO). Omit on first sync."),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Called by the client when it comes back online.
      - notifications: everything created after `since` PLUS every unresolved alert
        that was never acknowledged (delivered_at is null) - i.e. alerts raised while offline.
      - resolved_ids: alerts closed after `since` so the client can drop them locally.
      - server_time: store this and send it as `since` next time.
    The client should then call POST /notifications/sync/ack with the ids it stored.
    """
    server_time = datetime.now()
    base = db.query(Notification).filter(
        Notification.user_id == current_user.id, Notification.deleted_at.is_(None))

    cond = and_(Notification.is_resolved == False, Notification.delivered_at.is_(None))
    if since is not None:
        cond = or_(cond, Notification.created_at > since)
    rows = base.filter(cond).order_by(Notification.created_at.asc()).limit(limit).all()

    resolved_ids: List[int] = []
    if since is not None:
        resolved_ids = [r[0] for r in base.filter(
            Notification.is_resolved == True, Notification.resolved_at > since
        ).with_entities(Notification.id).all()]

    unread = base.filter(Notification.is_read == False, Notification.is_resolved == False).count()
    return {
        "server_time": server_time,
        "notifications": [_notif_dict(n) for n in rows],
        "resolved_ids": resolved_ids,
        "unread_count": unread,
        "has_more": len(rows) == limit,
    }


@router.post("/sync/ack")
async def ack_sync(
    body: SyncAckRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Client confirms it stored these notifications; they will not be re-sent as 'pending'."""
    if not body.ids:
        return {"acknowledged": 0}
    n = db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.id.in_(body.ids),
        Notification.delivered_at.is_(None),
    ).update({"delivered_at": datetime.now()}, synchronize_session=False)
    db.commit()
    return {"acknowledged": n}


# ─── Dashboard alert summary ────────────────────────────────────────────────

@router.get("/alerts/summary")
async def alert_summary(
    top: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Dashboard alert widget: open (unresolved) alerts of the current user by priority/type + top items."""
    base = db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.category == "alert",
        Notification.is_resolved == False,
        Notification.deleted_at.is_(None),
    )
    by_priority = {p: c for p, c in base.with_entities(
        Notification.priority, func.count(Notification.id)).group_by(Notification.priority).all()}
    by_type = {t: c for t, c in base.with_entities(
        Notification.notification_type, func.count(Notification.id)).group_by(Notification.notification_type).all()}
    escalated = base.filter(Notification.title.like(ESCALATED_PREFIX + "%")).count()
    unread = base.filter(Notification.is_read == False).count()

    weight = case(
        (Notification.priority == "urgent", 0),
        (Notification.priority == "high", 1),
        else_=2,
    )
    items = base.order_by(weight, Notification.created_at.desc()).limit(top).all()
    return {
        "total_open": sum(by_priority.values()),
        "unread_open": unread,
        "escalated_open": escalated,
        "by_priority": {"urgent": by_priority.get("urgent", 0), "high": by_priority.get("high", 0),
                        "normal": by_priority.get("normal", 0)},
        "by_type": by_type,
        "top_alerts": [_notif_dict(n) for n in items],
    }


# ─── Escalation log ─────────────────────────────────────────────────────────

@router.get("/escalation-logs")
async def get_escalation_logs(
    event_type: Optional[str] = None,
    level: Optional[str] = None,
    action: Optional[str] = Query(None, description="notified / escalated / closed"),
    alert_key: Optional[str] = None,
    pregnant_woman_id: Optional[int] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Escalation log: user, timestamp and action taken for every alert.
    District -> its whole district, Block -> its block, other roles -> only their own entries.
    """
    q = db.query(EscalationLog, User, PregnantWoman).outerjoin(
        User, EscalationLog.user_id == User.id).outerjoin(
        PregnantWoman, EscalationLog.pregnant_woman_id == PregnantWoman.id)

    role = getattr(current_user.role, "value", current_user.role)
    if role == "district":
        q = q.filter(or_(PregnantWoman.district_id == current_user.district_id,
                         EscalationLog.user_id == current_user.id))
    elif role == "block":
        q = q.filter(or_(PregnantWoman.block_id == current_user.block_id,
                         EscalationLog.user_id == current_user.id))
    else:
        q = q.filter(EscalationLog.user_id == current_user.id)

    if event_type:
        q = q.filter(EscalationLog.event_type == event_type)
    if level:
        q = q.filter(EscalationLog.level == level)
    if action:
        q = q.filter(EscalationLog.action == action)
    if alert_key:
        q = q.filter(EscalationLog.alert_key == alert_key)
    if pregnant_woman_id:
        q = q.filter(EscalationLog.pregnant_woman_id == pregnant_woman_id)
    if from_date:
        q = q.filter(EscalationLog.created_at >= datetime.combine(from_date, datetime.min.time()))
    if to_date:
        q = q.filter(EscalationLog.created_at <= datetime.combine(to_date, datetime.max.time()))

    total = q.count()
    rows = q.order_by(EscalationLog.created_at.desc(), EscalationLog.id.desc()).offset(skip).limit(limit).all()
    return {
        "total": total,
        "items": [
            {
                "id": log.id,
                "timestamp": log.created_at,
                "event_type": log.event_type,
                "level": log.level,
                "action": log.action,
                "action_note": log.action_note,
                "alert_key": log.alert_key,
                "user_id": log.user_id,
                "user_name": u.full_name if u else None,
                "user_role": getattr(u.role, "value", u.role) if u else None,
                "pregnant_woman_id": log.pregnant_woman_id,
                "pregnant_woman_name": pw.full_name if pw else None,
                "reference_type": log.reference_type,
                "reference_id": log.reference_id,
            }
            for log, u, pw in rows
        ],
    }


# ─── Push notification device tokens ────────────────────────────────────────

@router.post("/devices/register")
async def register_device(
    body: DeviceRegisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Register (or refresh) the FCM token of this device/browser for push notifications."""
    if body.platform not in ("web", "android", "ios"):
        raise HTTPException(status_code=400, detail="platform must be web, android or ios")
    if not body.token or len(body.token) > 512:
        raise HTTPException(status_code=400, detail="Invalid token")
    push_service.register_token(db, current_user.id, body.token, body.platform)
    return {"message": "Device registered", "push_configured": push_service.is_configured()}


@router.post("/devices/unregister")
async def unregister_device(
    body: DeviceUnregisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Call on logout so this device stops receiving the user's pushes."""
    n = push_service.unregister_token(db, current_user.id, body.token)
    return {"message": "Device unregistered", "count": n}


@router.delete("/clear-all")
async def clear_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Clear all read notifications.
    (Declared before DELETE /{notification_id}, otherwise 'clear-all' was parsed as an id -> 422.)
    """
    count = NotificationService.clear_all_read(db, current_user.id)
    return {"message": f"Cleared {count} read notifications", "count": count}


@router.get("/{notification_id}", response_model=NotificationResponse)
async def get_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get single notification details
    """
    notification = db.query(Notification).filter(
        Notification.id == notification_id,
        Notification.user_id == current_user.id,
        Notification.deleted_at.is_(None)
    ).first()
    
    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found"
        )
    
    # Parse extra_data
    extra_data = None
    if notification.extra_data:
        import json
        try:
            extra_data = json.loads(notification.extra_data)
        except:
            extra_data = None
    
    return {
        "id": notification.id,
        "user_id": notification.user_id,
        "title": notification.title,
        "message": notification.message,
        "notification_type": notification.notification_type,
        "category": notification.category,
        "priority": notification.priority,
        "reference_id": notification.reference_id,
        "reference_type": notification.reference_type,
        "action_url": notification.action_url,
        "extra_data": extra_data,
        "is_read": notification.is_read,
        "read_at": notification.read_at,
        "deleted_at": notification.deleted_at,
        "created_at": notification.created_at,
        "alert_key": notification.alert_key,
        "is_resolved": bool(notification.is_resolved),
        "resolved_at": notification.resolved_at,
        "escalation_level": notification.escalation_level,
    }

@router.put("/{notification_id}/read")
async def mark_as_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Mark notification as read
    """
    success = NotificationService.mark_as_read(db, notification_id, current_user.id)
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found or already read"
        )
    
    return {"message": "Notification marked as read"}

@router.put("/mark-all-read")
async def mark_all_as_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Mark all notifications as read
    """
    count = NotificationService.mark_all_as_read(db, current_user.id)
    return {"message": f"Marked {count} notifications as read", "count": count}

@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Delete single notification (soft delete)
    """
    success = NotificationService.delete_notification(db, notification_id, current_user.id)
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found"
        )
    
    return {"message": "Notification deleted successfully"}

