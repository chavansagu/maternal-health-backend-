from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional

from database import get_db
from models import User, Notification
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
            "created_at": notification.created_at
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
        "created_at": notification.created_at
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

@router.delete("/clear-all")
async def clear_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Clear all read notifications
    """
    count = NotificationService.clear_all_read(db, current_user.id)
    return {"message": f"Cleared {count} read notifications", "count": count}
