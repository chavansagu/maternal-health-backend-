"""
Enhanced Tracking (28 weeks and above) - list + summary for District and Block users.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from auth import get_current_active_user
from database import get_db
from models import User
from services.enhanced_tracking_service import TAB_LEVELS, get_enhanced_tracking

router = APIRouter(prefix="/enhanced-tracking", tags=["Enhanced Tracking"])

ALLOWED_ROLES = ("block", "district")


def _check_role(user: User):
    if user.role not in ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="Enhanced tracking is available to block and district users only")


@router.get("")
async def list_enhanced_tracking(
    level: Optional[str] = Query(None, description="enhanced | high_priority | critical (empty = all)"),
    search: Optional[str] = Query(None),
    block_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _check_role(current_user)
    if level and level not in TAB_LEVELS:
        raise HTTPException(status_code=400, detail=f"level must be one of {', '.join(TAB_LEVELS)}")
    return get_enhanced_tracking(db, current_user, level=level, search=search,
                                 block_id=block_id, page=page, per_page=per_page)


@router.get("/summary")
async def enhanced_tracking_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Counts only - used by the two dashboard cards."""
    _check_role(current_user)
    return get_enhanced_tracking(db, current_user, page=1, per_page=1)["summary"]