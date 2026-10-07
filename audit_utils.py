"""
Audit Logging Utilities
Tracks user actions, authentication events, and data changes
"""
from sqlalchemy.orm import Session
from fastapi import Request
from typing import Optional, Dict, Any
from datetime import datetime
import json
import os
from dotenv import load_dotenv

load_dotenv()

AUDIT_ENABLED = os.getenv("AUDIT_ENABLED", "true").lower() == "true"
AUDIT_LOG_VIEWS = os.getenv("AUDIT_LOG_VIEWS", "false").lower() == "true"

def get_client_info(request: Request) -> tuple:
    """Extract IP address and User-Agent from request"""
    ip_address = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    return ip_address, user_agent

def serialize_values(data: Any) -> str:
    """Serialize data to JSON string, handling non-serializable objects"""
    if data is None:
        return None
    try:
        return json.dumps(data, default=str)
    except:
        return str(data)

def log_audit(
    db: Session,
    user_id: Optional[int],
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    old_values: Optional[Dict] = None,
    new_values: Optional[Dict] = None,
    ip_address: str = "unknown",
    user_agent: str = "unknown"
):
    """Core audit logging function"""
    if not AUDIT_ENABLED:
        return
    
    from models import AuditLog
    
    audit_log = AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_values=serialize_values(old_values),
        new_values=serialize_values(new_values),
        ip_address=ip_address,
        user_agent=user_agent[:255] if user_agent else "unknown",
        created_at=datetime.now()
    )
    
    db.add(audit_log)
    db.commit()

def log_login(db: Session, user_id: int, success: bool, ip_address: str, user_agent: str):
    """Log login attempt"""
    action = "LOGIN_SUCCESS" if success else "LOGIN_FAILED"
    log_audit(db, user_id, action, "User", user_id, None, None, ip_address, user_agent)

def log_logout(db: Session, user_id: int, ip_address: str, user_agent: str):
    """Log logout"""
    log_audit(db, user_id, "LOGOUT", "User", user_id, None, None, ip_address, user_agent)

def log_password_change(db: Session, user_id: int, ip_address: str, user_agent: str):
    """Log password change"""
    log_audit(db, user_id, "PASSWORD_CHANGE", "User", user_id, None, None, ip_address, user_agent)

def log_password_reset(db: Session, user_id: int, ip_address: str, user_agent: str):
    """Log password reset"""
    log_audit(db, user_id, "PASSWORD_RESET", "User", user_id, None, None, ip_address, user_agent)

def log_create(
    db: Session,
    user_id: int,
    entity_type: str,
    entity_id: int,
    new_values: Dict,
    ip_address: str,
    user_agent: str
):
    """Log entity creation"""
    log_audit(db, user_id, "CREATE", entity_type, entity_id, None, new_values, ip_address, user_agent)

def log_update(
    db: Session,
    user_id: int,
    entity_type: str,
    entity_id: int,
    old_values: Dict,
    new_values: Dict,
    ip_address: str,
    user_agent: str
):
    """Log entity update"""
    log_audit(db, user_id, "UPDATE", entity_type, entity_id, old_values, new_values, ip_address, user_agent)

def log_delete(
    db: Session,
    user_id: int,
    entity_type: str,
    entity_id: int,
    old_values: Dict,
    ip_address: str,
    user_agent: str
):
    """Log entity deletion"""
    log_audit(db, user_id, "DELETE", entity_type, entity_id, old_values, None, ip_address, user_agent)

def log_view(
    db: Session,
    user_id: int,
    entity_type: str,
    entity_id: int,
    ip_address: str,
    user_agent: str
):
    """Log entity view (optional, controlled by AUDIT_LOG_VIEWS)"""
    if AUDIT_LOG_VIEWS:
        log_audit(db, user_id, "VIEW", entity_type, entity_id, None, None, ip_address, user_agent)

def log_bulk_action(
    db: Session,
    user_id: int,
    action: str,
    entity_type: str,
    count: int,
    summary: Dict,
    ip_address: str,
    user_agent: str
):
    """Log bulk operations"""
    log_audit(db, user_id, action, entity_type, None, None, summary, ip_address, user_agent)

def get_entity_snapshot(entity) -> Dict:
    """Get current state of entity as dict"""
    if entity is None:
        return {}
    
    # Exclude sensitive fields
    exclude_fields = {'password_hash', 'token'}
    
    snapshot = {}
    for column in entity.__table__.columns:
        if column.name not in exclude_fields:
            value = getattr(entity, column.name)
            snapshot[column.name] = value
    
    return snapshot
