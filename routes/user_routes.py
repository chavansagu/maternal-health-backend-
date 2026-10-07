from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from typing import List

from database import get_db
from models import User, DeliveryPoint
from schemas import UserCreate, UserResponse, UserUpdate
from auth import get_password_hash, get_current_active_user, get_district_user
from audit_utils import (
    get_client_info,
    get_entity_snapshot,
    log_create,
    log_update,
    log_delete,
    log_audit
)
from services.notification_service import NotificationService

router = APIRouter(prefix="/users", tags=["User Management"])

def _sync_ward_subcentre_mapping(db: Session, sub_centre_id, ward_ids):
    """Make sure every ward assigned to a sub-centre user is also present in
    ward_subcentre_mapping, which is what PW registration/listing validate against."""
    from models import WardSubcentreMapping
    if not sub_centre_id or not ward_ids:
        return
    for wid in set(ward_ids):
        exists = db.query(WardSubcentreMapping.id).filter(
            WardSubcentreMapping.ward_id == wid,
            WardSubcentreMapping.sub_centre_id == sub_centre_id,
        ).first()
        if not exists:
            db.add(WardSubcentreMapping(ward_id=wid, sub_centre_id=sub_centre_id))


@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    user_data: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_district_user)  # Only district users can create users
):
    """
    Create a new user (District admin only)
    """
    # Check if username already exists
    existing_user = db.query(User).filter(User.username == user_data.username).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists"
        )
    
    # Check if email already exists (if provided)
    if user_data.email:
        existing_email = db.query(User).filter(User.email == user_data.email).first()
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already exists"
            )
    
    # A user can only be assigned to an existing, ACTIVE Delivery Point
    if user_data.dp_id is not None:
        dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == user_data.dp_id).first()
        if not dp:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Delivery Point not found"
            )
        if not dp.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot assign a user to an inactive Delivery Point"
            )

    # Create new user
    new_user = User(
        username=user_data.username,
        email=user_data.email,
        password_hash=get_password_hash(user_data.password),
        role=user_data.role.name,
        full_name=user_data.full_name,
        mobile_number=user_data.mobile_number,
        district_id=user_data.district_id,
        block_id=user_data.block_id,
        sub_centre_id=user_data.sub_centre_id,
        usg_centre_id=user_data.usg_centre_id,
        dp_id=user_data.dp_id,
        pmsma_centre_id=user_data.pmsma_centre_id,
        created_by=current_user.id
    )
    
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    # Add ward mappings if provided (for sub-centre users)
    if user_data.ward_ids and user_data.role == "sub_centre":
        from models import UserWardMapping, Ward
        
        for ward_id in user_data.ward_ids:
            # Verify ward exists and belongs to the same block
            ward = db.query(Ward).filter(Ward.id == ward_id).first()
            if not ward:
                db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Ward {ward_id} not found"
                )
            
            if user_data.block_id and ward.block_id != user_data.block_id:
                db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Ward {ward_id} does not belong to the specified block"
                )
            
            # Create mapping
            mapping = UserWardMapping(
                user_id=new_user.id,
                ward_id=ward_id
            )
            db.add(mapping)
        
        _sync_ward_subcentre_mapping(db, new_user.sub_centre_id, user_data.ward_ids)
        db.commit()

    ip_address, user_agent = get_client_info(request)
    log_create(
        db,
        current_user.id,
        "User",
        new_user.id,
        get_entity_snapshot(new_user),
        ip_address,
        user_agent
    )
    
    # Send notification to newly created user
    NotificationService.create_notification(
        db=db,
        user_ids=[new_user.id],
        title="Welcome to Janani Jyoti",
        message=f"Your account has been created successfully. Username: {new_user.username}",
        notification_type="user_created",
        category="user_management",
        priority="normal",
        reference_id=new_user.id,
        reference_type="user",
        action_url="/profile",
        metadata={"username": new_user.username, "role": new_user.role}
    )
    
    return new_user

@router.get("/")
async def get_users(
    skip: int = 0,
    limit: int = 100,
    role: str = None,
    is_active: bool = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_district_user)  # Only district (Admin) users can list users
):
    """
    Get list of users with optional filters
    """
    from models import UserWardMapping
    
    query = db.query(User)
    
    # Apply filters based on current user's role
    if current_user.role == "district":
        query = query.filter(User.district_id == current_user.district_id)
    elif current_user.role == "block":
        query = query.filter(User.block_id == current_user.block_id)
    
    # Additional filters
    if role:
        query = query.filter(User.role == role)
    if is_active is not None:
        query = query.filter(User.is_active == is_active)
    
    users = query.offset(skip).limit(limit).all()
    
    # Add ward_ids for sub-centre users
    result = []
    for user in users:
        user_data = {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "full_name": user.full_name,
            "mobile_number": user.mobile_number,
            "district_id": user.district_id,
            "block_id": user.block_id,
            "sub_centre_id": user.sub_centre_id,
            "usg_centre_id": user.usg_centre_id,
            "pmsma_centre_id": user.pmsma_centre_id,
            "is_active": user.is_active,
            "created_at": user.created_at,
            "updated_at": user.updated_at
        }
        
        # Add ward_ids for sub-centre users
        if user.role == "sub_centre":
            ward_mappings = db.query(UserWardMapping.ward_id).filter(
                UserWardMapping.user_id == user.id
            ).all()
            user_data["ward_ids"] = [w.ward_id for w in ward_mappings] if ward_mappings else []
        
        result.append(user_data)
    
    return result

@router.get("/{user_id}")
async def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_district_user)  # Only district (Admin) users can view user details
):
    """
    Get user by ID with entity names
    """
    from models import UserWardMapping, District, Block, SubCentre, USGCentre, Ward, PMSMACentre
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Check authorization
    if current_user.role == "district" and user.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and user.block_id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Get entity names
    district_name = None
    if user.district_id:
        district = db.query(District).filter(District.id == user.district_id).first()
        district_name = district.name if district else None
    
    block_name = None
    if user.block_id:
        block = db.query(Block).filter(Block.id == user.block_id).first()
        block_name = block.name if block else None
    
    sub_centre_name = None
    if user.sub_centre_id:
        sub_centre = db.query(SubCentre).filter(SubCentre.id == user.sub_centre_id).first()
        sub_centre_name = sub_centre.name if sub_centre else None
    
    usg_centre_name = None
    if user.usg_centre_id:
        usg_centre = db.query(USGCentre).filter(USGCentre.id == user.usg_centre_id).first()
        usg_centre_name = usg_centre.name if usg_centre else None
    
    pmsma_centre_name = None
    if user.pmsma_centre_id:
        pmsma_centre = db.query(PMSMACentre).filter(PMSMACentre.id == user.pmsma_centre_id).first()
        pmsma_centre_name = pmsma_centre.name if pmsma_centre else None
    
    # Build response
    user_data = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "full_name": user.full_name,
        "mobile_number": user.mobile_number,
        "district_id": user.district_id,
        "district_name": district_name,
        "block_id": user.block_id,
        "block_name": block_name,
        "sub_centre_id": user.sub_centre_id,
        "sub_centre_name": sub_centre_name,
        "usg_centre_id": user.usg_centre_id,
        "usg_centre_name": usg_centre_name,
        "pmsma_centre_id": user.pmsma_centre_id,
        "pmsma_centre_name": pmsma_centre_name,
        "is_active": user.is_active,
        "created_at": user.created_at,
        "updated_at": user.updated_at
    }
    
    # Add ward_ids and ward_names for sub-centre users
    if user.role == "sub_centre":
        ward_mappings = db.query(UserWardMapping, Ward).join(
            Ward, UserWardMapping.ward_id == Ward.id
        ).filter(UserWardMapping.user_id == user.id).all()
        
        if ward_mappings:
            user_data["ward_ids"] = [mapping.Ward.id for mapping in ward_mappings]
            user_data["ward_names"] = [mapping.Ward.name for mapping in ward_mappings]
        else:
            user_data["ward_ids"] = []
            user_data["ward_names"] = []
    
    return user_data

@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_update: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_district_user)  # Only district users can update
):
    """
    Update user information (District admin only)
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Update fields
    old_values = get_entity_snapshot(user)
    update_data = user_update.dict(exclude_unset=True)
    
    # Check if is_active is being changed
    is_active_changed = "is_active" in update_data and update_data["is_active"] != user.is_active
    new_is_active = update_data.get("is_active")
    
    for field, value in update_data.items():
        setattr(user, field, value)
    
    db.commit()
    db.refresh(user)

    ip_address, user_agent = get_client_info(request)
    log_update(
        db,
        current_user.id,
        "User",
        user.id,
        old_values,
        get_entity_snapshot(user),
        ip_address,
        user_agent
    )
    
    # Send notification if account status changed
    if is_active_changed:
        if new_is_active:
            NotificationService.create_notification(
                db=db,
                user_ids=[user.id],
                title="Account Activated",
                message="Your account has been activated. You can now access the system.",
                notification_type="account_activated",
                category="user_management",
                priority="normal",
                reference_id=user.id,
                reference_type="user",
                action_url="/profile",
                metadata={"username": user.username}
            )
        else:
            NotificationService.create_notification(
                db=db,
                user_ids=[user.id],
                title="Account Deactivated",
                message="Your account has been deactivated. Please contact administrator for assistance.",
                notification_type="account_deactivated",
                category="user_management",
                priority="high",
                reference_id=user.id,
                reference_type="user",
                action_url="/profile",
                metadata={"username": user.username}
            )
    
    # Send notification for other profile updates
    elif update_data:
        NotificationService.create_notification(
            db=db,
            user_ids=[user.id],
            title="Profile Updated",
            message="Your profile information has been updated.",
            notification_type="permissions_updated",
            category="user_management",
            priority="normal",
            reference_id=user.id,
            reference_type="user",
            action_url="/profile",
            metadata={"username": user.username, "updated_fields": list(update_data.keys())}
        )
    
    return user

@router.delete("/{user_id}")
async def deactivate_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_district_user)
):
    """
    Deactivate user (District admin only)
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    old_values = get_entity_snapshot(user)
    user.is_active = False
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_delete(
        db,
        current_user.id,
        "User",
        user.id,
        old_values,
        ip_address,
        user_agent
    )
    
    # Send notification to deactivated user
    NotificationService.create_notification(
        db=db,
        user_ids=[user.id],
        title="Account Deactivated",
        message="Your account has been deactivated by administrator.",
        notification_type="account_deactivated",
        category="user_management",
        priority="high",
        reference_id=user.id,
        reference_type="user",
        action_url="/profile",
        metadata={"username": user.username}
    )
    
    return {"message": "User deactivated successfully"}

@router.post("/{user_id}/activate")
async def activate_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_district_user)
):
    """
    Activate a deactivated user (District admin only)
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    old_values = get_entity_snapshot(user)
    user.is_active = True
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_audit(
        db,
        current_user.id,
        "ACTIVATE",
        "User",
        user.id,
        old_values,
        get_entity_snapshot(user),
        ip_address,
        user_agent
    )
    
    return {"message": "User activated successfully"}

@router.get("/{user_id}/wards")
async def get_user_wards(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get wards assigned to a sub-centre user
    """
    from models import UserWardMapping, Ward
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    
    if user.role != "sub_centre":
        return {"message": "User is not a sub-centre user", "wards": []}
    
    # Get assigned wards
    mappings = db.query(UserWardMapping, Ward).join(
        Ward, UserWardMapping.ward_id == Ward.id
    ).filter(UserWardMapping.user_id == user_id).all()
    
    wards = [{
        "ward_id": mapping.Ward.id,
        "ward_name": mapping.Ward.name,
        "ward_code": mapping.Ward.code,
        "block_id": mapping.Ward.block_id
    } for mapping in mappings]
    
    return {"user_id": user_id, "wards": wards}

@router.put("/{user_id}/wards")
async def update_user_wards(
    user_id: int,
    ward_ids: List[int],
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_district_user)
):
    """
    Update wards assigned to a sub-centre user (District admin only)
    """
    from models import UserWardMapping, Ward
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    
    if user.role != "sub_centre":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can only assign wards to sub-centre users"
        )
    
    # Delete existing mappings
    db.query(UserWardMapping).filter(UserWardMapping.user_id == user_id).delete()
    
    # Add new mappings
    for ward_id in ward_ids:
        ward = db.query(Ward).filter(Ward.id == ward_id).first()
        if not ward:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Ward {ward_id} not found"
            )
        
        if user.block_id and ward.block_id != user.block_id:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Ward {ward_id} does not belong to user's block"
            )
        
        mapping = UserWardMapping(user_id=user_id, ward_id=ward_id)
        db.add(mapping)
    
    _sync_ward_subcentre_mapping(db, user.sub_centre_id, ward_ids)
    db.commit()
    
    ip_address, user_agent = get_client_info(request)
    log_audit(
        db,
        current_user.id,
        "UPDATE_WARDS",
        "User",
        user.id,
        None,
        {"ward_ids": ward_ids},
        ip_address,
        user_agent
    )
    
    return {"message": "User wards updated successfully", "ward_ids": ward_ids}