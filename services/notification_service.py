"""
Notification Service
Handles notification creation, delivery, and management
"""
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from datetime import datetime
import json

from models import Notification, User

class NotificationService:
    
    @staticmethod
    def create_notification(
        db: Session,
        user_ids: List[int],
        title: str,
        message: str,
        notification_type: str,
        category: str = "system",
        priority: str = "normal",
        reference_id: Optional[int] = None,
        reference_type: Optional[str] = None,
        action_url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> List[Notification]:
        """Create notifications for multiple users"""
        notifications = []
        
        metadata_str = json.dumps(metadata) if metadata else None
        
        for user_id in user_ids:
            notification = Notification(
                user_id=user_id,
                title=title,
                message=message,
                notification_type=notification_type,
                category=category,
                priority=priority,
                reference_id=reference_id,
                reference_type=reference_type,
                action_url=action_url,
                extra_data=metadata_str
            )
            db.add(notification)
            notifications.append(notification)
        
        db.commit()
        for notification in notifications:
            db.refresh(notification)
        
        return notifications
    
    @staticmethod
    def get_recipients_for_event(
        db: Session,
        event_type: str,
        context: Dict[str, Any]
    ) -> List[int]:
        """Determine who should receive notification based on event type and context"""
        recipients = []
        
        # USG Appointment Events
        if event_type in ["appointment_scheduled", "appointment_emergency"]:
            usg_centre_id = context.get("usg_centre_id")
            if usg_centre_id:
                users = db.query(User).filter(
                    User.usg_centre_id == usg_centre_id,
                    User.is_active == True
                ).all()
                recipients = [u.id for u in users]
        
        # Grievance Events
        elif event_type == "grievance_new":
            block_id = context.get("block_id")
            if block_id:
                users = db.query(User).filter(
                    User.block_id == block_id,
                    User.role == "block",
                    User.is_active == True
                ).all()
                recipients = [u.id for u in users]
        
        elif event_type == "grievance_escalated":
            district_id = context.get("district_id")
            if district_id:
                users = db.query(User).filter(
                    User.district_id == district_id,
                    User.role == "district",
                    User.is_active == True
                ).all()
                recipients = [u.id for u in users]
        
        # Registration Events
        elif event_type == "registration_pending":
            ward_id = context.get("ward_id")
            sub_centre_id = context.get("sub_centre_id")
            
            if ward_id:
                from models import UserWardMapping
                user_ids = db.query(UserWardMapping.user_id).filter(
                    UserWardMapping.ward_id == ward_id
                ).all()
                recipients = [uid[0] for uid in user_ids]
            
            if not recipients and sub_centre_id:
                users = db.query(User).filter(
                    User.sub_centre_id == sub_centre_id,
                    User.role == "sub_centre",
                    User.is_active == True
                ).all()
                recipients = [u.id for u in users]
        
        elif event_type == "high_risk_alert":
            district_id = context.get("district_id")
            block_id = context.get("block_id")
            sub_centre_id = context.get("sub_centre_id")
            
            users = db.query(User).filter(
                User.is_active == True,
                (
                    (User.district_id == district_id) & (User.role == "district") |
                    (User.block_id == block_id) & (User.role == "block") |
                    (User.sub_centre_id == sub_centre_id) & (User.role == "sub_centre")
                )
            ).all()
            recipients = [u.id for u in users]
        
        # Bulk Upload Events
        elif event_type == "bulk_upload_completed":
            uploader_id = context.get("uploader_id")
            if uploader_id:
                recipients = [uploader_id]
        
        # Appointment Status Events
        elif event_type in ["appointment_accepted", "appointment_completed", "report_uploaded"]:
            scheduled_by = context.get("scheduled_by")
            if scheduled_by:
                recipients = [scheduled_by]
        
        elif event_type in ["appointment_rescheduled", "appointment_cancelled"]:
            scheduled_by = context.get("scheduled_by")
            usg_centre_id = context.get("usg_centre_id")
            
            if scheduled_by:
                recipients.append(scheduled_by)
            
            if usg_centre_id:
                users = db.query(User).filter(
                    User.usg_centre_id == usg_centre_id,
                    User.is_active == True
                ).all()
                recipients.extend([u.id for u in users])
        
        # Overdue Events
        elif event_type in ["appointment_overdue", "grievance_overdue"]:
            district_id = context.get("district_id")
            block_id = context.get("block_id")
            
            users = db.query(User).filter(
                User.is_active == True,
                (
                    (User.district_id == district_id) & (User.role == "district") |
                    (User.block_id == block_id) & (User.role == "block")
                )
            ).all()
            recipients = [u.id for u in users]
        
        # User Management Events
        elif event_type in ["user_created", "account_activated", "account_deactivated", "permissions_updated"]:
            user_id = context.get("user_id")
            if user_id:
                recipients = [user_id]
        
        return list(set(recipients))  # Remove duplicates
    
    @staticmethod
    def get_notifications(
        db: Session,
        user_id: int,
        skip: int = 0,
        limit: int = 20,
        is_read: Optional[bool] = None,
        category: Optional[str] = None,
        priority: Optional[str] = None
    ) -> List[Notification]:
        """Get user's notifications with filters"""
        query = db.query(Notification).filter(
            Notification.user_id == user_id,
            Notification.deleted_at.is_(None)
        )
        
        if is_read is not None:
            query = query.filter(Notification.is_read == is_read)
        if category:
            query = query.filter(Notification.category == category)
        if priority:
            query = query.filter(Notification.priority == priority)
        
        return query.order_by(Notification.created_at.desc()).offset(skip).limit(limit).all()
    
    @staticmethod
    def get_unread_count(db: Session, user_id: int) -> int:
        """Get count of unread notifications"""
        return db.query(Notification).filter(
            Notification.user_id == user_id,
            Notification.is_read == False,
            Notification.deleted_at.is_(None)
        ).count()
    
    @staticmethod
    def mark_as_read(db: Session, notification_id: int, user_id: int) -> bool:
        """Mark notification as read"""
        notification = db.query(Notification).filter(
            Notification.id == notification_id,
            Notification.user_id == user_id
        ).first()
        
        if notification and not notification.is_read:
            notification.is_read = True
            notification.read_at = datetime.now()
            db.commit()
            return True
        return False
    
    @staticmethod
    def mark_all_as_read(db: Session, user_id: int) -> int:
        """Mark all notifications as read for user"""
        count = db.query(Notification).filter(
            Notification.user_id == user_id,
            Notification.is_read == False,
            Notification.deleted_at.is_(None)
        ).update({
            "is_read": True,
            "read_at": datetime.now()
        })
        db.commit()
        return count
    
    @staticmethod
    def delete_notification(db: Session, notification_id: int, user_id: int) -> bool:
        """Soft delete notification"""
        notification = db.query(Notification).filter(
            Notification.id == notification_id,
            Notification.user_id == user_id
        ).first()
        
        if notification:
            notification.deleted_at = datetime.now()
            db.commit()
            return True
        return False
    
    @staticmethod
    def clear_all_read(db: Session, user_id: int) -> int:
        """Clear all read notifications"""
        count = db.query(Notification).filter(
            Notification.user_id == user_id,
            Notification.is_read == True,
            Notification.deleted_at.is_(None)
        ).update({"deleted_at": datetime.now()})
        db.commit()
        return count
    
    @staticmethod
    def get_statistics(db: Session, user_id: int) -> Dict[str, Any]:
        """Get notification statistics for user"""
        notifications = db.query(Notification).filter(
            Notification.user_id == user_id,
            Notification.deleted_at.is_(None)
        ).all()
        
        total = len(notifications)
        unread = sum(1 for n in notifications if not n.is_read)
        
        by_category = {}
        by_priority = {}
        
        for n in notifications:
            by_category[n.category] = by_category.get(n.category, 0) + 1
            by_priority[n.priority] = by_priority.get(n.priority, 0) + 1
        
        return {
            "total": total,
            "unread": unread,
            "by_category": by_category,
            "by_priority": by_priority
        }
