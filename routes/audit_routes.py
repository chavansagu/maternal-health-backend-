from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from typing import Optional, List
from datetime import datetime, timedelta

from database import get_db
from models import AuditLog, User
from auth import get_current_active_user

router = APIRouter(prefix="/audit", tags=["Audit Logs"])

@router.get("/filters")
async def get_audit_filter_values(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get distinct entity types and actions for filtering (District/Block users only)"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="Access denied")
    
    query = db.query(AuditLog)
    
    # Role-based filtering
    if current_user.role == "block":
        block_user_ids = db.query(User.id).filter(User.block_id == current_user.block_id).subquery()
        query = query.filter(AuditLog.user_id.in_(block_user_ids))
    elif current_user.role == "district":
        district_user_ids = db.query(User.id).filter(User.district_id == current_user.district_id).subquery()
        query = query.filter(AuditLog.user_id.in_(district_user_ids))
    
    entity_types = [
        row[0] for row in query.with_entities(AuditLog.entity_type).distinct().all()
        if row[0]
    ]
    actions = [
        row[0] for row in query.with_entities(AuditLog.action).distinct().all()
        if row[0]
    ]
    
    return {
        "entity_types": sorted(entity_types),
        "actions": sorted(actions)
    }

@router.get("/logs")
async def get_audit_logs(
    skip: int = 0,
    limit: int = 100,
    user_id: Optional[int] = None,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get audit logs with filters (District/Block users only)"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="Access denied")
    
    query = db.query(AuditLog)
    
    # Apply filters
    if user_id:
        query = query.filter(AuditLog.user_id == user_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if entity_id:
        query = query.filter(AuditLog.entity_id == entity_id)
    if start_date:
        query = query.filter(AuditLog.created_at >= start_date)
    if end_date:
        query = query.filter(AuditLog.created_at <= end_date)
    
    # Role-based filtering
    if current_user.role == "block":
        # Block users can only see logs from their block users
        block_user_ids = db.query(User.id).filter(User.block_id == current_user.block_id).subquery()
        query = query.filter(AuditLog.user_id.in_(block_user_ids))
    elif current_user.role == "district":
        # District users can see logs from their district users
        district_user_ids = db.query(User.id).filter(User.district_id == current_user.district_id).subquery()
        query = query.filter(AuditLog.user_id.in_(district_user_ids))
    
    total = query.count()
    logs = query.order_by(desc(AuditLog.created_at)).offset(skip).limit(limit).all()
    
    return {
        "total": total,
        "page": skip // limit + 1,
        "page_size": limit,
        "logs": logs
    }

@router.get("/logs/{log_id}")
async def get_audit_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get single audit log details"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="Access denied")
    
    log = db.query(AuditLog).filter(AuditLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Audit log not found")
    
    return log

@router.get("/user/{user_id}/activity")
async def get_user_activity(
    user_id: int,
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get activity history for a specific user"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="Access denied")
    
    start_date = datetime.now() - timedelta(days=days)
    
    logs = db.query(AuditLog).filter(
        AuditLog.user_id == user_id,
        AuditLog.created_at >= start_date
    ).order_by(desc(AuditLog.created_at)).all()
    
    # Get user info
    user = db.query(User).filter(User.id == user_id).first()
    
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role
        } if user else None,
        "activity_count": len(logs),
        "period_days": days,
        "logs": logs
    }

@router.get("/entity/{entity_type}/{entity_id}/history")
async def get_entity_history(
    entity_type: str,
    entity_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get change history for a specific entity"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="Access denied")
    
    logs = db.query(AuditLog).filter(
        AuditLog.entity_type == entity_type,
        AuditLog.entity_id == entity_id
    ).order_by(desc(AuditLog.created_at)).all()
    
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "change_count": len(logs),
        "history": logs
    }

@router.get("/statistics/summary")
async def get_audit_statistics(
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get audit statistics summary"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="Access denied")
    
    start_date = datetime.now() - timedelta(days=days)
    
    query = db.query(AuditLog).filter(AuditLog.created_at >= start_date)
    
    # Role-based filtering
    if current_user.role == "block":
        block_user_ids = db.query(User.id).filter(User.block_id == current_user.block_id).subquery()
        query = query.filter(AuditLog.user_id.in_(block_user_ids))
    elif current_user.role == "district":
        district_user_ids = db.query(User.id).filter(User.district_id == current_user.district_id).subquery()
        query = query.filter(AuditLog.user_id.in_(district_user_ids))
    
    total_actions = query.count()
    
    # Actions by type
    actions_by_type = db.query(
        AuditLog.action,
        func.count(AuditLog.id).label('count')
    ).filter(AuditLog.created_at >= start_date).group_by(AuditLog.action).all()
    
    # Most active users
    most_active = db.query(
        AuditLog.user_id,
        func.count(AuditLog.id).label('action_count')
    ).filter(AuditLog.created_at >= start_date).group_by(AuditLog.user_id).order_by(desc('action_count')).limit(10).all()
    
    # Get user details for most active
    active_users = []
    for user_id, count in most_active:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            active_users.append({
                "user_id": user_id,
                "username": user.username,
                "full_name": user.full_name,
                "action_count": count
            })
    
    return {
        "period_days": days,
        "total_actions": total_actions,
        "actions_by_type": [{"action": a, "count": c} for a, c in actions_by_type],
        "most_active_users": active_users
    }

@router.get("/recent")
async def get_recent_activities(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get recent audit activities"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="Access denied")
    
    query = db.query(AuditLog)
    
    # Role-based filtering
    if current_user.role == "block":
        block_user_ids = db.query(User.id).filter(User.block_id == current_user.block_id).subquery()
        query = query.filter(AuditLog.user_id.in_(block_user_ids))
    elif current_user.role == "district":
        district_user_ids = db.query(User.id).filter(User.district_id == current_user.district_id).subquery()
        query = query.filter(AuditLog.user_id.in_(district_user_ids))
    
    logs = query.order_by(desc(AuditLog.created_at)).limit(limit).all()
    
    return {"recent_activities": logs}
