from fastapi import APIRouter, Depends, HTTPException, status, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import csv
import io
import json
import logging
import traceback

from database import get_db
from models import District, Block, Ward, SubCentre, USGCentre, WardSubcentreMapping, BlockSubcentreMapping, USGCentreBlockMapping, User, PregnantWoman, USGAppointment, PMSMACentre, DeliveryPoint, BulkUpload
from schemas import (
    DistrictCreate, DistrictUpdate, DistrictResponse,
    BlockCreate, BlockUpdate, BlockResponse,
    WardCreate, WardUpdate, WardResponse,
    SubCentreCreate, SubCentreUpdate, SubCentreResponse,
    USGCentreCreate, USGCentreUpdate, USGCentreResponse,
    WardMappingCreate, WardMappingResponse, BlockMappingCreate, BlockMappingResponse,
    PMSMACentreCreate, PMSMACentreUpdate, PMSMACentreResponse
)
from auth import get_current_active_user, get_district_user
from audit_utils import (
    get_client_info,
    get_entity_snapshot,
    log_create,
    log_update,
    log_delete,
    log_audit,
    log_bulk_action
)
from services.notification_service import NotificationService
import admin_bulk_upload_service as admin_bulk

logger = logging.getLogger(__name__)

# router = APIRouter(prefix="/admin", tags=["Administrative Management"])
router = APIRouter(prefix="/admin")

# Helper Functions
def check_entity_dependencies(db: Session, entity_type: str, entity_id: int) -> dict:
    """Check if entity has dependencies before deletion"""
    dependencies = {"can_delete": True, "dependencies": []}
    
    if entity_type == "district":
        blocks_count = db.query(Block).filter(Block.district_id == entity_id, Block.is_active == True).count()
        if blocks_count > 0:
            dependencies["can_delete"] = False
            dependencies["dependencies"].append(f"{blocks_count} active blocks")
            
    elif entity_type == "block":
        wards_count = db.query(Ward).filter(Ward.block_id == entity_id, Ward.is_active == True).count()
        subcentres_count = db.query(SubCentre).filter(SubCentre.block_id == entity_id, SubCentre.is_active == True).count()
        if wards_count > 0 or subcentres_count > 0:
            dependencies["can_delete"] = False
            if wards_count > 0:
                dependencies["dependencies"].append(f"{wards_count} active wards")
            if subcentres_count > 0:
                dependencies["dependencies"].append(f"{subcentres_count} active sub-centres")
                
    elif entity_type == "ward":
        pw_count = db.query(PregnantWoman).filter(PregnantWoman.ward_id == entity_id, PregnantWoman.is_active == True).count()
        if pw_count > 0:
            dependencies["can_delete"] = False
            dependencies["dependencies"].append(f"{pw_count} active pregnant women")
            
    elif entity_type == "sub_centre":
        users_count = db.query(User).filter(User.sub_centre_id == entity_id, User.is_active == True).count()
        pw_count = db.query(PregnantWoman).filter(PregnantWoman.sub_centre_id == entity_id, PregnantWoman.is_active == True).count()
        if users_count > 0 or pw_count > 0:
            dependencies["can_delete"] = False
            if users_count > 0:
                dependencies["dependencies"].append(f"{users_count} active users")
            if pw_count > 0:
                dependencies["dependencies"].append(f"{pw_count} active pregnant women")
                
    elif entity_type == "usg_centre":
        users_count = db.query(User).filter(User.usg_centre_id == entity_id, User.is_active == True).count()
        appointments_count = db.query(USGAppointment).filter(
            USGAppointment.usg_centre_id == entity_id,
            USGAppointment.status.in_(["scheduled", "accepted"])
        ).count()
        if users_count > 0 or appointments_count > 0:
            dependencies["can_delete"] = False
            if users_count > 0:
                dependencies["dependencies"].append(f"{users_count} active users")
            if appointments_count > 0:
                dependencies["dependencies"].append(f"{appointments_count} pending appointments")
    
    return dependencies

def check_code_uniqueness(db: Session, model_class, code: str, district_id: int = None, block_id: int = None, exclude_id: int = None) -> bool:
    """Check if code is unique within the scope"""
    query = db.query(model_class).filter(model_class.code == code)
    
    if district_id:
        query = query.filter(model_class.district_id == district_id)
    if block_id:
        query = query.filter(model_class.block_id == block_id)
    if exclude_id:
        query = query.filter(model_class.id != exclude_id)
        
    return query.first() is None

# DISTRICT MANAGEMENT
@router.post("/districts", response_model=DistrictResponse, status_code=status.HTTP_201_CREATED, tags=["District Management"])
async def create_district(
    district_data: DistrictCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_district_user)
):
    """Create a new district (District admin only)"""
    # Check code uniqueness
    if not check_code_uniqueness(db, District, district_data.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="District code already exists"
        )
    
    district = District(**district_data.dict())
    db.add(district)
    db.commit()
    db.refresh(district)

    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "District", district.id, get_entity_snapshot(district), ip_address, user_agent)
    return district

@router.get("/districts", response_model=List[DistrictResponse], tags=["District Management"])
async def get_districts(
    skip: int = 0,
    limit: int = 100,
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """Get all districts"""
    query = db.query(District)
    if is_active is not None:
        query = query.filter(District.is_active == is_active)
    return query.offset(skip).limit(limit).all()

@router.get("/districts/{district_id}", response_model=DistrictResponse, tags=["District Management"])
async def get_district(
    district_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get district by ID"""
    district = db.query(District).filter(District.id == district_id).first()
    if not district:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="District not found")
    return district

@router.put("/districts/{district_id}", response_model=DistrictResponse, tags=["District Management"])
async def update_district(
    district_id: int,
    district_update: DistrictUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_district_user)
):
    """Update district (District admin only)"""
    district = db.query(District).filter(District.id == district_id).first()
    if not district:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="District not found")
    
    # Check code uniqueness if code is being updated
    if district_update.code and district_update.code != district.code:
        if not check_code_uniqueness(db, District, district_update.code, exclude_id=district_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="District code already exists"
            )
    
    old_values = get_entity_snapshot(district)
    update_data = district_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(district, field, value)
    
    district.updated_at = datetime.now()
    db.commit()
    db.refresh(district)

    ip_address, user_agent = get_client_info(request)
    log_update(db, current_user.id, "District", district.id, old_values, get_entity_snapshot(district), ip_address, user_agent)
    return district

@router.delete("/districts/{district_id}", tags=["District Management"])
async def delete_district(
    district_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_district_user)
):
    """Deactivate district (District admin only)"""
    district = db.query(District).filter(District.id == district_id).first()
    if not district:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="District not found")
    
    # Check dependencies
    dependencies = check_entity_dependencies(db, "district", district_id)
    if not dependencies["can_delete"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete district. Dependencies: {', '.join(dependencies['dependencies'])}"
        )
    
    old_values = get_entity_snapshot(district)
    district.is_active = False
    district.updated_at = datetime.now()
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_delete(db, current_user.id, "District", district.id, old_values, ip_address, user_agent)
    return {"message": "District deactivated successfully"}

@router.post("/districts/{district_id}/activate", tags=["District Management"])
async def activate_district(
    district_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_district_user)
):
    """Reactivate district (District admin only)"""
    district = db.query(District).filter(District.id == district_id).first()
    if not district:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="District not found")
    
    old_values = get_entity_snapshot(district)
    district.is_active = True
    district.updated_at = datetime.now()
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_audit(db, current_user.id, "ACTIVATE", "District", district.id, old_values, get_entity_snapshot(district), ip_address, user_agent)
    return {"message": "District activated successfully"}

# BLOCK MANAGEMENT
@router.post("/blocks", response_model=BlockResponse, status_code=status.HTTP_201_CREATED, tags=["Block Management"])
async def create_block(
    block_data: BlockCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Create a new block"""
    # Permission check - allow district users to create blocks in any district, or in their own district
    if current_user.role == "district":
        # If user has a district_id, they can only create blocks in their district
        if current_user.district_id and current_user.district_id != block_data.district_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to create block in this district")
    elif current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Check if district exists and is active
    district = db.query(District).filter(
        District.id == block_data.district_id,
        District.is_active == True
    ).first()
    if not district:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="District not found or inactive"
        )
    
    # Check code uniqueness within district
    if not check_code_uniqueness(db, Block, block_data.code, district_id=block_data.district_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Block code already exists in this district"
        )
    
    block = Block(**block_data.dict())
    db.add(block)
    db.commit()
    db.refresh(block)

    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "Block", block.id, get_entity_snapshot(block), ip_address, user_agent)
    
    # Notify district users about new block
    if block.district_id:
        district_users = db.query(User).filter(
            User.district_id == block.district_id,
            User.role == "district",
            User.is_active == True
        ).all()
        
        if district_users:
            NotificationService.create_notification(
                db=db,
                user_ids=[u.id for u in district_users],
                title="New Block Created",
                message=f"Block '{block.name}' has been created in your district",
                notification_type="admin_entity_created",
                category="admin",
                priority="low",
                reference_id=block.id,
                reference_type="block",
                action_url=f"/admin/blocks/{block.id}",
                metadata={"block_name": block.name, "entity_type": "block"}
            )
    
    return block

@router.get("/blocks", response_model=List[BlockResponse], tags=["Block Management"])
async def get_blocks(
    skip: int = 0,
    limit: int = 100,
    district_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """Get blocks with filters"""
    query = db.query(Block)
    
    if district_id:
        query = query.filter(Block.district_id == district_id)
    if is_active is not None:
        query = query.filter(Block.is_active == is_active)
    
    return query.offset(skip).limit(limit).all()

@router.get("/blocks/{block_id}", response_model=BlockResponse, tags=["Block Management"])
async def get_block(
    block_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get block by ID"""
    block = db.query(Block).filter(Block.id == block_id).first()
    if not block:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")
    
    # Permission check
    if current_user.role == "district" and current_user.district_id and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and block.id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    return block

@router.put("/blocks/{block_id}", response_model=BlockResponse, tags=["Block Management"])
async def update_block(
    block_id: int,
    block_update: BlockUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Update block"""
    block = db.query(Block).filter(Block.id == block_id).first()
    if not block:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")
    
    # Permission check
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    if current_user.role == "district" and current_user.district_id and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    if current_user.role == "block" and block.id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Check code uniqueness if code is being updated
    if block_update.code and block_update.code != block.code:
        if not check_code_uniqueness(db, Block, block_update.code, district_id=block.district_id, exclude_id=block_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Block code already exists in this district"
            )
    
    old_values = get_entity_snapshot(block)
    update_data = block_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(block, field, value)
    
    block.updated_at = datetime.now()
    db.commit()
    db.refresh(block)

    ip_address, user_agent = get_client_info(request)
    log_update(db, current_user.id, "Block", block.id, old_values, get_entity_snapshot(block), ip_address, user_agent)
    return block

@router.delete("/blocks/{block_id}", tags=["Block Management"])
async def delete_block(
    block_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Deactivate block"""
    block = db.query(Block).filter(Block.id == block_id).first()
    if not block:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")
    
    # Permission check
    if current_user.role not in ["district"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    if current_user.district_id and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Check dependencies
    dependencies = check_entity_dependencies(db, "block", block_id)
    if not dependencies["can_delete"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete block. Dependencies: {', '.join(dependencies['dependencies'])}"
        )
    
    old_values = get_entity_snapshot(block)
    block.is_active = False
    block.updated_at = datetime.now()
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_delete(db, current_user.id, "Block", block.id, old_values, ip_address, user_agent)
    
    # Notify affected users
    affected_users = db.query(User).filter(
        User.block_id == block_id,
        User.is_active == True
    ).all()
    
    if affected_users:
        NotificationService.create_notification(
            db=db,
            user_ids=[u.id for u in affected_users],
            title="Block Deactivated",
            message=f"Block '{block.name}' has been deactivated",
            notification_type="admin_entity_deactivated",
            category="admin",
            priority="normal",
            reference_id=block.id,
            reference_type="block",
            action_url=f"/admin/blocks/{block.id}",
            metadata={"block_name": block.name, "entity_type": "block"}
        )
    
    return {"message": "Block deactivated successfully"}

# Continue with Ward, SubCentre, and USG Centre management...
# (This is getting long, so I'll create the rest in the next part)

# WARD MANAGEMENT
@router.post("/wards", response_model=WardResponse, status_code=status.HTTP_201_CREATED, tags=["Ward Management"])
async def create_ward(
    ward_data: WardCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Create a new ward"""
    # Permission check
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Check if block exists and is active
    block = db.query(Block).filter(
        Block.id == ward_data.block_id,
        Block.is_active == True
    ).first()
    if not block:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Block not found or inactive"
        )
    
    # Permission check for district user
    if current_user.role == "district" and current_user.district_id and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and block.id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Check code uniqueness within block
    if not check_code_uniqueness(db, Ward, ward_data.code, block_id=ward_data.block_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ward code already exists in this block"
        )
    
    ward = Ward(**ward_data.dict())
    db.add(ward)
    db.commit()
    db.refresh(ward)

    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "Ward", ward.id, get_entity_snapshot(ward), ip_address, user_agent)
    return ward

@router.get("/wards", response_model=List[WardResponse], tags=["Ward Management"])
async def get_wards(
    skip: int = 0,
    limit: int = 100,
    block_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """Get wards with filters"""
    query = db.query(Ward)
    
    if block_id:
        query = query.filter(Ward.block_id == block_id)
    if is_active is not None:
        query = query.filter(Ward.is_active == is_active)
    
    return query.offset(skip).limit(limit).all()

@router.get("/wards/{ward_id}", response_model=WardResponse, tags=["Ward Management"])
async def get_ward(
    ward_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get ward by ID"""
    ward = db.query(Ward).filter(Ward.id == ward_id).first()
    if not ward:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ward not found")
    return ward

@router.put("/wards/{ward_id}", response_model=WardResponse, tags=["Ward Management"])
async def update_ward(
    ward_id: int,
    ward_update: WardUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Update ward"""
    ward = db.query(Ward).filter(Ward.id == ward_id).first()
    if not ward:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ward not found")
    
    # Permission check
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    block = db.query(Block).filter(Block.id == ward.block_id).first()
    if current_user.role == "district" and current_user.district_id and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and ward.block_id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    old_values = get_entity_snapshot(ward)
    update_data = ward_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(ward, field, value)
    
    db.commit()
    db.refresh(ward)

    ip_address, user_agent = get_client_info(request)
    log_update(db, current_user.id, "Ward", ward.id, old_values, get_entity_snapshot(ward), ip_address, user_agent)
    return ward

@router.delete("/wards/{ward_id}", tags=["Ward Management"])
async def delete_ward(
    ward_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Deactivate ward"""
    ward = db.query(Ward).filter(Ward.id == ward_id).first()
    if not ward:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ward not found")
    
    # Permission check
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    block = db.query(Block).filter(Block.id == ward.block_id).first()
    if current_user.role == "district" and current_user.district_id and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and ward.block_id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Check dependencies
    dependencies = check_entity_dependencies(db, "ward", ward_id)
    if not dependencies["can_delete"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete ward. Dependencies: {', '.join(dependencies['dependencies'])}"
        )
    
    old_values = get_entity_snapshot(ward)
    ward.is_active = False
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_delete(db, current_user.id, "Ward", ward.id, old_values, ip_address, user_agent)
    return {"message": "Ward deactivated successfully"}

# SUB-CENTRE MANAGEMENT
@router.post("/sub-centres", response_model=SubCentreResponse, status_code=status.HTTP_201_CREATED, tags=["Sub-Centre Management"])
async def create_sub_centre(
    subcentre_data: SubCentreCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Create a new sub-centre"""
    # Permission check
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Check if block exists and is active
    block = db.query(Block).filter(
        Block.id == subcentre_data.block_id,
        Block.is_active == True
    ).first()
    if not block:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Block not found or inactive"
        )
    
    # Permission check
    if current_user.role == "district" and current_user.district_id and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and block.id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Check code uniqueness within block
    if not check_code_uniqueness(db, SubCentre, subcentre_data.code, block_id=subcentre_data.block_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sub-centre code already exists in this block"
        )
    
    subcentre = SubCentre(**subcentre_data.dict())
    db.add(subcentre)
    db.commit()
    db.refresh(subcentre)

    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "SubCentre", subcentre.id, get_entity_snapshot(subcentre), ip_address, user_agent)
    
    # Notify block users about new sub-centre
    if subcentre.block_id:
        block_users = db.query(User).filter(
            User.block_id == subcentre.block_id,
            User.role == "block",
            User.is_active == True
        ).all()
        
        if block_users:
            NotificationService.create_notification(
                db=db,
                user_ids=[u.id for u in block_users],
                title="New Sub-Centre Created",
                message=f"Sub-Centre '{subcentre.name}' has been created in your block",
                notification_type="admin_entity_created",
                category="admin",
                priority="low",
                reference_id=subcentre.id,
                reference_type="sub_centre",
                action_url=f"/admin/sub-centres/{subcentre.id}",
                metadata={"sub_centre_name": subcentre.name, "entity_type": "sub_centre"}
            )
    
    return subcentre

@router.get("/sub-centres", response_model=List[SubCentreResponse], tags=["Sub-Centre Management"])
async def get_sub_centres(
    skip: int = 0,
    limit: int = 100,
    block_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """Get sub-centres with filters"""
    query = db.query(SubCentre)
    
    if block_id:
        query = query.filter(SubCentre.block_id == block_id)
    if is_active is not None:
        query = query.filter(SubCentre.is_active == is_active)
    
    return query.offset(skip).limit(limit).all()

@router.get("/sub-centres/{subcentre_id}", response_model=SubCentreResponse, tags=["Sub-Centre Management"])
async def get_sub_centre(
    subcentre_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get sub-centre by ID"""
    subcentre = db.query(SubCentre).filter(SubCentre.id == subcentre_id).first()
    if not subcentre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-centre not found")
    return subcentre

@router.put("/sub-centres/{subcentre_id}", response_model=SubCentreResponse, tags=["Sub-Centre Management"])
async def update_sub_centre(
    subcentre_id: int,
    subcentre_update: SubCentreUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Update sub-centre"""
    subcentre = db.query(SubCentre).filter(SubCentre.id == subcentre_id).first()
    if not subcentre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-centre not found")
    
    # Permission check
    if current_user.role not in ["district", "block", "sub_centre"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    block = db.query(Block).filter(Block.id == subcentre.block_id).first()
    if current_user.role == "district" and current_user.district_id and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and subcentre.block_id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "sub_centre" and subcentre.id != current_user.sub_centre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    old_values = get_entity_snapshot(subcentre)
    update_data = subcentre_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(subcentre, field, value)
    
    subcentre.updated_at = datetime.now()
    db.commit()
    db.refresh(subcentre)

    ip_address, user_agent = get_client_info(request)
    log_update(db, current_user.id, "SubCentre", subcentre.id, old_values, get_entity_snapshot(subcentre), ip_address, user_agent)
    return subcentre

@router.delete("/sub-centres/{subcentre_id}", tags=["Sub-Centre Management"])
async def delete_sub_centre(
    subcentre_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Deactivate sub-centre"""
    subcentre = db.query(SubCentre).filter(SubCentre.id == subcentre_id).first()
    if not subcentre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-centre not found")
    
    # Permission check
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Check dependencies
    dependencies = check_entity_dependencies(db, "sub_centre", subcentre_id)
    if not dependencies["can_delete"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete sub-centre. Dependencies: {', '.join(dependencies['dependencies'])}"
        )
    
    old_values = get_entity_snapshot(subcentre)
    subcentre.is_active = False
    subcentre.updated_at = datetime.now()
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_delete(db, current_user.id, "SubCentre", subcentre.id, old_values, ip_address, user_agent)
    
    # Notify affected users
    affected_users = db.query(User).filter(
        User.sub_centre_id == subcentre_id,
        User.is_active == True
    ).all()
    
    if affected_users:
        NotificationService.create_notification(
            db=db,
            user_ids=[u.id for u in affected_users],
            title="Sub-Centre Deactivated",
            message=f"Sub-Centre '{subcentre.name}' has been deactivated",
            notification_type="admin_entity_deactivated",
            category="admin",
            priority="normal",
            reference_id=subcentre.id,
            reference_type="sub_centre",
            action_url=f"/admin/sub-centres/{subcentre.id}",
            metadata={"sub_centre_name": subcentre.name, "entity_type": "sub_centre"}
        )
    
    return {"message": "Sub-centre deactivated successfully"}

# USG CENTRE MANAGEMENT
@router.post("/usg-centres", response_model=USGCentreResponse, status_code=status.HTTP_201_CREATED, tags=["USG Centre Management"])
async def create_usg_centre(
    usgcentre_data: USGCentreCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Create a new USG centre (District admin only)"""
    # Permission check - only district users can create USG centres
    if current_user.role != "district":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only district users can create USG centres")
    
    # Check if district exists and is active
    if usgcentre_data.district_id:
        district = db.query(District).filter(
            District.id == usgcentre_data.district_id,
            District.is_active == True
        ).first()
        if not district:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="District not found or inactive"
            )
        
        # Permission check - if user has district_id, they can only create in their district
        if current_user.district_id and current_user.district_id != usgcentre_data.district_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to create USG centre in this district")
    
    # Check code uniqueness within district
    if not check_code_uniqueness(db, USGCentre, usgcentre_data.code, district_id=usgcentre_data.district_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="USG centre code already exists in this district"
        )
    
    # Extract block_ids before creating USG centre
    block_ids = usgcentre_data.block_ids or []
    
    # Set primary block_id (first block in list)
    if block_ids:
        usgcentre_data.block_id = block_ids[0]
    
    # Create USG centre (exclude block_ids from dict)
    usgcentre_dict = usgcentre_data.dict(exclude={'block_ids'})
    usgcentre = USGCentre(**usgcentre_dict)
    db.add(usgcentre)
    db.commit()
    db.refresh(usgcentre)
    
    # Create block mappings
    if block_ids:
        for block_id in block_ids:
            # Verify block exists
            block = db.query(Block).filter(Block.id == block_id).first()
            if not block:
                db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Block {block_id} not found"
                )
            
            # Create mapping
            mapping = USGCentreBlockMapping(
                usg_centre_id=usgcentre.id,
                block_id=block_id
            )
            db.add(mapping)
        
        db.commit()

    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "USGCentre", usgcentre.id, get_entity_snapshot(usgcentre), ip_address, user_agent)
    return usgcentre

@router.get("/usg-centres", response_model=List[USGCentreResponse], tags=["USG Centre Management"])
async def get_usg_centres(
    skip: int = 0,
    limit: int = 100,
    district_id: Optional[int] = None,
    block_id: Optional[int] = None,
    ward_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    is_private: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """Get USG centres with filters"""
    query = db.query(USGCentre)
    
    if district_id:
        query = query.filter(USGCentre.district_id == district_id)
    if block_id:
        query = query.join(USGCentreBlockMapping,
                           USGCentreBlockMapping.usg_centre_id == USGCentre.id)\
                     .filter(USGCentreBlockMapping.block_id == block_id)
    if ward_id:
        query = query.filter(USGCentre.ward_id == ward_id)
    if is_active is not None:
        query = query.filter(USGCentre.is_active == is_active)
    if is_private is not None:
        query = query.filter(USGCentre.is_private == is_private)
    
    usg_centres = query.offset(skip).limit(limit).all()
    
    # Add block_ids to each USG centre
    for centre in usg_centres:
        block_ids = db.query(USGCentreBlockMapping.block_id)\
                     .filter(USGCentreBlockMapping.usg_centre_id == centre.id)\
                     .all()
        centre.block_ids = [block_id[0] for block_id in block_ids] if block_ids else None
    
    return usg_centres

@router.get("/usg-centres/{usgcentre_id}", response_model=USGCentreResponse, tags=["USG Centre Management"])
async def get_usg_centre(
    usgcentre_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get USG centre by ID"""
    usgcentre = db.query(USGCentre).filter(USGCentre.id == usgcentre_id).first()
    if not usgcentre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="USG centre not found")
    
    # Add block_ids to response
    block_ids = db.query(USGCentreBlockMapping.block_id)\
                 .filter(USGCentreBlockMapping.usg_centre_id == usgcentre_id)\
                 .all()
    usgcentre.block_ids = [block_id[0] for block_id in block_ids] if block_ids else None
    
    return usgcentre

@router.put("/usg-centres/{usgcentre_id}", response_model=USGCentreResponse, tags=["USG Centre Management"])
async def update_usg_centre(
    usgcentre_id: int,
    usgcentre_update: USGCentreUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Update USG centre"""
    usgcentre = db.query(USGCentre).filter(USGCentre.id == usgcentre_id).first()
    if not usgcentre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="USG centre not found")
    
    # Permission check
    if current_user.role not in ["district", "usg_centre"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    if current_user.role == "district" and current_user.district_id and usgcentre.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "usg_centre" and usgcentre.id != current_user.usg_centre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    old_values = get_entity_snapshot(usgcentre)
    
    # Handle block_ids update
    block_ids = usgcentre_update.block_ids
    if block_ids is not None:
        # Delete existing mappings
        db.query(USGCentreBlockMapping).filter(
            USGCentreBlockMapping.usg_centre_id == usgcentre_id
        ).delete()
        
        # Create new mappings
        for block_id in block_ids:
            # Verify block exists
            block = db.query(Block).filter(Block.id == block_id).first()
            if not block:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Block {block_id} not found"
                )
            
            mapping = USGCentreBlockMapping(
                usg_centre_id=usgcentre_id,
                block_id=block_id
            )
            db.add(mapping)
        
        # Update primary block_id if blocks provided
        if block_ids:
            usgcentre_update.block_id = block_ids[0]
    
    # Update USG centre fields (exclude block_ids)
    update_data = usgcentre_update.dict(exclude_unset=True, exclude={'block_ids'})
    for field, value in update_data.items():
        setattr(usgcentre, field, value)
    
    usgcentre.updated_at = datetime.now()
    db.commit()
    db.refresh(usgcentre)
    
    # Add block_ids to response
    block_ids_result = db.query(USGCentreBlockMapping.block_id)\
                        .filter(USGCentreBlockMapping.usg_centre_id == usgcentre_id)\
                        .all()
    usgcentre.block_ids = [block_id[0] for block_id in block_ids_result] if block_ids_result else None

    ip_address, user_agent = get_client_info(request)
    log_update(db, current_user.id, "USGCentre", usgcentre.id, old_values, get_entity_snapshot(usgcentre), ip_address, user_agent)
    return usgcentre

@router.delete("/usg-centres/{usgcentre_id}", tags=["USG Centre Management"])
async def delete_usg_centre(
    usgcentre_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Deactivate USG centre"""
    usgcentre = db.query(USGCentre).filter(USGCentre.id == usgcentre_id).first()
    if not usgcentre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="USG centre not found")
    
    # Permission check - only district users can delete USG centres in their district
    if current_user.role != "district":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only district users can delete USG centres")
    
    if current_user.district_id and usgcentre.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to delete USG centre in this district")
    
    # Check dependencies
    dependencies = check_entity_dependencies(db, "usg_centre", usgcentre_id)
    if not dependencies["can_delete"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete USG centre. Dependencies: {', '.join(dependencies['dependencies'])}"
        )
    
    old_values = get_entity_snapshot(usgcentre)
    usgcentre.is_active = False
    usgcentre.updated_at = datetime.now()
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_delete(db, current_user.id, "USGCentre", usgcentre.id, old_values, ip_address, user_agent)
    return {"message": "USG centre deactivated successfully"}

# WARD-SUBCENTRE MAPPING
@router.get("/sub-centres/{subcentre_id}/wards", response_model=List[WardMappingResponse], tags=["Ward-SubCentre Mapping"])
async def get_ward_mappings(
    subcentre_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get all ward mappings for a sub-centre"""
    # Check if sub-centre exists
    subcentre = db.query(SubCentre).filter(SubCentre.id == subcentre_id).first()
    if not subcentre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-centre not found")
    
    # Get all mappings for this sub-centre
    mappings = db.query(WardSubcentreMapping).filter(
        WardSubcentreMapping.sub_centre_id == subcentre_id
    ).all()
    
    return mappings

@router.post("/sub-centres/{subcentre_id}/wards", response_model=List[WardMappingResponse], tags=["Ward-SubCentre Mapping"])
async def map_wards_to_subcentre(
    subcentre_id: int,
    ward_mapping: WardMappingCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Map wards to sub-centre"""
    # Check if sub-centre exists
    subcentre = db.query(SubCentre).filter(SubCentre.id == subcentre_id).first()
    if not subcentre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-centre not found")
    
    # Permission check
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    mappings = []
    for ward_id in ward_mapping.ward_ids:
        # Check if ward exists and belongs to same block
        ward = db.query(Ward).filter(
            Ward.id == ward_id,
            Ward.block_id == subcentre.block_id
        ).first()
        if not ward:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Ward {ward_id} not found or not in same block"
            )
        
        # Check if mapping already exists
        existing_mapping = db.query(WardSubcentreMapping).filter(
            WardSubcentreMapping.ward_id == ward_id,
            WardSubcentreMapping.sub_centre_id == subcentre_id
        ).first()
        
        if not existing_mapping:
            mapping = WardSubcentreMapping(
                ward_id=ward_id,
                sub_centre_id=subcentre_id
            )
            db.add(mapping)
            mappings.append(mapping)
    
    db.commit()
    for mapping in mappings:
        db.refresh(mapping)

    ip_address, user_agent = get_client_info(request)
    log_bulk_action(
        db,
        current_user.id,
        "MAP_WARDS",
        "WardSubcentreMapping",
        len(mappings),
        {"sub_centre_id": subcentre_id, "ward_ids": ward_mapping.ward_ids},
        ip_address,
        user_agent
    )
    
    return mappings

@router.delete("/sub-centres/{subcentre_id}/wards/{ward_id}", tags=["Ward-SubCentre Mapping"])
async def remove_ward_mapping(
    subcentre_id: int,
    ward_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Remove ward mapping from sub-centre"""
    mapping = db.query(WardSubcentreMapping).filter(
        WardSubcentreMapping.ward_id == ward_id,
        WardSubcentreMapping.sub_centre_id == subcentre_id
    ).first()
    
    if not mapping:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")
    
    old_values = get_entity_snapshot(mapping)
    db.delete(mapping)
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_delete(db, current_user.id, "WardSubcentreMapping", mapping.id, old_values, ip_address, user_agent)
    return {"message": "Ward mapping removed successfully"}

# BLOCK-SUBCENTRE MAPPING
@router.get("/sub-centres/{subcentre_id}/blocks", response_model=List[BlockMappingResponse], tags=["Block-SubCentre Mapping"])
async def get_block_mappings(
    subcentre_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get all block mappings for a sub-centre"""
    # Check if sub-centre exists
    subcentre = db.query(SubCentre).filter(SubCentre.id == subcentre_id).first()
    if not subcentre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-centre not found")
    
    # Get all mappings for this sub-centre
    mappings = db.query(BlockSubcentreMapping).filter(
        BlockSubcentreMapping.sub_centre_id == subcentre_id
    ).all()
    
    return mappings

@router.post("/sub-centres/{subcentre_id}/blocks", response_model=List[BlockMappingResponse], tags=["Block-SubCentre Mapping"])
async def map_blocks_to_subcentre(
    subcentre_id: int,
    block_mapping: BlockMappingCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Map blocks to sub-centre"""
    # Check if sub-centre exists
    subcentre = db.query(SubCentre).filter(SubCentre.id == subcentre_id).first()
    if not subcentre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-centre not found")
    
    # Permission check
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    mappings = []
    for block_id in block_mapping.block_ids:
        # Check if block exists
        block = db.query(Block).filter(Block.id == block_id).first()
        if not block:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Block {block_id} not found"
            )
        
        # Check if mapping already exists
        existing_mapping = db.query(BlockSubcentreMapping).filter(
            BlockSubcentreMapping.block_id == block_id,
            BlockSubcentreMapping.sub_centre_id == subcentre_id
        ).first()
        
        if not existing_mapping:
            mapping = BlockSubcentreMapping(
                block_id=block_id,
                sub_centre_id=subcentre_id
            )
            db.add(mapping)
            mappings.append(mapping)
    
    db.commit()
    for mapping in mappings:
        db.refresh(mapping)

    ip_address, user_agent = get_client_info(request)
    log_bulk_action(
        db,
        current_user.id,
        "MAP_BLOCKS",
        "BlockSubcentreMapping",
        len(mappings),
        {"sub_centre_id": subcentre_id, "block_ids": block_mapping.block_ids},
        ip_address,
        user_agent
    )
    
    return mappings

@router.delete("/sub-centres/{subcentre_id}/blocks/{block_id}", tags=["Block-SubCentre Mapping"])
async def remove_block_mapping(
    subcentre_id: int,
    block_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Remove block mapping from sub-centre"""
    mapping = db.query(BlockSubcentreMapping).filter(
        BlockSubcentreMapping.block_id == block_id,
        BlockSubcentreMapping.sub_centre_id == subcentre_id
    ).first()
    
    if not mapping:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")
    
    old_values = get_entity_snapshot(mapping)
    db.delete(mapping)
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_delete(db, current_user.id, "BlockSubcentreMapping", mapping.id, old_values, ip_address, user_agent)
    return {"message": "Block mapping removed successfully"}


# DELIVERY POINT MANAGEMENT
from models import DeliveryPoint
from schemas import DeliveryPointCreate, DeliveryPointUpdate, DeliveryPointResponse

@router.post("/delivery-points", response_model=DeliveryPointResponse, status_code=status.HTTP_201_CREATED, tags=["Delivery Point Management"])
async def create_delivery_point(
    dp_data: DeliveryPointCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Create a new Delivery Point (District admin only)"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="Only district users can create Delivery Points")

    if current_user.district_id and current_user.district_id != dp_data.district_id:
        raise HTTPException(status_code=403, detail="Not authorized to create Delivery Point in this district")

    existing = db.query(DeliveryPoint).filter(DeliveryPoint.code == dp_data.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Delivery Point code already exists")

    dp = DeliveryPoint(**dp_data.dict())
    db.add(dp)
    db.commit()
    db.refresh(dp)

    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "DeliveryPoint", dp.id, get_entity_snapshot(dp), ip_address, user_agent)
    return dp


@router.get("/delivery-points", response_model=List[DeliveryPointResponse], tags=["Delivery Point Management"])
async def get_delivery_points(
    skip: int = 0,
    limit: int = 100,
    district_id: Optional[int] = None,
    block_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """Get Delivery Points with filters"""
    query = db.query(DeliveryPoint)
    if district_id:
        query = query.filter(DeliveryPoint.district_id == district_id)
    if block_id:
        query = query.filter(DeliveryPoint.block_id == block_id)
    if is_active is not None:
        query = query.filter(DeliveryPoint.is_active == is_active)
    return query.offset(skip).limit(limit).all()


@router.get("/delivery-points/{dp_id}", response_model=DeliveryPointResponse, tags=["Delivery Point Management"])
async def get_delivery_point(
    dp_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get Delivery Point by ID"""
    dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == dp_id).first()
    if not dp:
        raise HTTPException(status_code=404, detail="Delivery Point not found")
    return dp


@router.put("/delivery-points/{dp_id}", response_model=DeliveryPointResponse, tags=["Delivery Point Management"])
async def update_delivery_point(
    dp_id: int,
    dp_update: DeliveryPointUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Update Delivery Point"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="Only district users can update Delivery Points")

    dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == dp_id).first()
    if not dp:
        raise HTTPException(status_code=404, detail="Delivery Point not found")

    if current_user.district_id and dp.district_id != current_user.district_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    old_values = get_entity_snapshot(dp)
    for field, value in dp_update.dict(exclude_unset=True).items():
        setattr(dp, field, value)
    dp.updated_at = datetime.now()
    db.commit()
    db.refresh(dp)

    ip_address, user_agent = get_client_info(request)
    log_update(db, current_user.id, "DeliveryPoint", dp.id, old_values, get_entity_snapshot(dp), ip_address, user_agent)
    return dp


@router.delete("/delivery-points/{dp_id}", tags=["Delivery Point Management"])
async def delete_delivery_point(
    dp_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Deactivate Delivery Point"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="Only district users can delete Delivery Points")

    dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == dp_id).first()
    if not dp:
        raise HTTPException(status_code=404, detail="Delivery Point not found")

    if dp.code == "OTHER_DP_GLOBAL":
        raise HTTPException(status_code=403, detail="Other DP Point cannot be deleted")

    if current_user.district_id and dp.district_id != current_user.district_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    old_values = get_entity_snapshot(dp)
    dp.is_active = False
    dp.updated_at = datetime.now()
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_delete(db, current_user.id, "DeliveryPoint", dp.id, old_values, ip_address, user_agent)
    return {"message": "Delivery Point deactivated successfully"}


# PMSMA CENTRE MANAGEMENT
@router.post("/pmsma-centres", response_model=PMSMACentreResponse, status_code=status.HTTP_201_CREATED, tags=["PMSMA Centre Management"])
async def create_pmsma_centre(
    data: PMSMACentreCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    if db.query(PMSMACentre).filter(PMSMACentre.code == data.code).first():
        raise HTTPException(status_code=400, detail="PMSMA centre code already exists")
    centre = PMSMACentre(**data.dict())
    db.add(centre)
    db.commit()
    db.refresh(centre)
    ip_address, user_agent = get_client_info(request)
    log_create(db, current_user.id, "PMSMACentre", centre.id, get_entity_snapshot(centre), ip_address, user_agent)
    return centre

@router.get("/pmsma-centres", response_model=List[PMSMACentreResponse], tags=["PMSMA Centre Management"])
async def get_pmsma_centres(
    skip: int = 0,
    limit: int = 100,
    district_id: Optional[int] = None,
    block_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    query = db.query(PMSMACentre)
    if district_id:
        query = query.filter(PMSMACentre.district_id == district_id)
    if block_id:
        query = query.filter(PMSMACentre.block_id == block_id)
    if is_active is not None:
        query = query.filter(PMSMACentre.is_active == is_active)
    return query.offset(skip).limit(limit).all()

@router.get("/pmsma-centres/{centre_id}", response_model=PMSMACentreResponse, tags=["PMSMA Centre Management"])
async def get_pmsma_centre(
    centre_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    centre = db.query(PMSMACentre).filter(PMSMACentre.id == centre_id).first()
    if not centre:
        raise HTTPException(status_code=404, detail="PMSMA centre not found")
    return centre

@router.put("/pmsma-centres/{centre_id}", response_model=PMSMACentreResponse, tags=["PMSMA Centre Management"])
async def update_pmsma_centre(
    centre_id: int,
    data: PMSMACentreUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    centre = db.query(PMSMACentre).filter(PMSMACentre.id == centre_id).first()
    if not centre:
        raise HTTPException(status_code=404, detail="PMSMA centre not found")
    old_values = get_entity_snapshot(centre)
    for field, value in data.dict(exclude_unset=True).items():
        setattr(centre, field, value)
    centre.updated_at = datetime.now()
    db.commit()
    db.refresh(centre)
    ip_address, user_agent = get_client_info(request)
    log_update(db, current_user.id, "PMSMACentre", centre.id, old_values, get_entity_snapshot(centre), ip_address, user_agent)
    return centre

@router.delete("/pmsma-centres/{centre_id}", tags=["PMSMA Centre Management"])
async def delete_pmsma_centre(
    centre_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    centre = db.query(PMSMACentre).filter(PMSMACentre.id == centre_id).first()
    if not centre:
        raise HTTPException(status_code=404, detail="PMSMA centre not found")
    old_values = get_entity_snapshot(centre)
    centre.is_active = False
    centre.updated_at = datetime.now()
    db.commit()
    ip_address, user_agent = get_client_info(request)
    log_delete(db, current_user.id, "PMSMACentre", centre.id, old_values, ip_address, user_agent)
    return {"message": "PMSMA centre deactivated successfully"}


# ─────────────────────────────────────────────
# BULK UPLOAD — TEMPLATE DOWNLOADS
# ─────────────────────────────────────────────

def _csv_response(rows: list[dict], filename: str) -> StreamingResponse:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@router.get("/bulk-upload/template/blocks", tags=["Bulk Upload"])
async def download_blocks_template(current_user: User = Depends(get_current_active_user)):
    return _csv_response([
        {"name": "Example Block", "name_regional": "ଉଦାହରଣ ବ୍ଲକ", "code": "BLK001", "district_name": "Puri"}
    ], "blocks_template.csv")

@router.get("/bulk-upload/template/wards", tags=["Bulk Upload"])
async def download_wards_template(current_user: User = Depends(get_current_active_user)):
    return _csv_response([
        {"name": "Example Village", "name_regional": "ଉଦାହରଣ ଗ୍ରାମ", "code": "WRD001", "block_name": "Astarang"}
    ], "wards_template.csv")

@router.get("/bulk-upload/template/sub-centres", tags=["Bulk Upload"])
async def download_subcentres_template(current_user: User = Depends(get_current_active_user)):
    return _csv_response([
        {"name": "Example HSC", "code": "HSC001", "block_name": "Astarang", "address": "Village Road", "contact_number": "9876543210"}
    ], "sub_centres_template.csv")

@router.get("/bulk-upload/template/usg-centres", tags=["Bulk Upload"])
async def download_usgcentres_template(current_user: User = Depends(get_current_active_user)):
    return _csv_response([
        {"name": "Example USG Centre", "code": "USG001", "district_name": "Puri", "block_names": "Astarang,Satyabadi",
         "address": "Main Road", "contact_number": "9876543210", "contact_person_name": "Dr. Example",
         "email": "usg@example.com", "is_private": "false"}
    ], "usg_centres_template.csv")

@router.get("/bulk-upload/template/delivery-points", tags=["Bulk Upload"])
async def download_deliverypoints_template(current_user: User = Depends(get_current_active_user)):
    return _csv_response([
        {"name": "Example DHH", "code": "DP001", "district_name": "Puri", "block_name": "Astarang",
         "address": "Hospital Road", "contact_number": "9876543210", "contact_person_name": "Dr. Example",
         "is_sdh_dhh": "false"}
    ], "delivery_points_template.csv")

@router.get("/bulk-upload/template/pmsma-centres", tags=["Bulk Upload"])
async def download_pmsmacentres_template(current_user: User = Depends(get_current_active_user)):
    return _csv_response([
        {"name": "Example PMSMA Centre", "code": "PMSMA001", "district_name": "Puri", "block_name": "Astarang",
         "address": "Main Road", "contact_number": "9876543210", "contact_person_name": "Dr. Example"}
    ], "pmsma_centres_template.csv")


# ─────────────────────────────────────────────
# BULK UPLOAD — validate -> preview -> confirm
# (same flow as pregnant-women bulk upload; logic lives in admin_bulk_upload_service.py)
# ─────────────────────────────────────────────

BULK_ENTITIES = tuple(admin_bulk.ENTITIES.keys())


def _bulk_checks(entity: str, file: UploadFile, current_user: User):
    if entity not in admin_bulk.ENTITIES:
        raise HTTPException(status_code=404, detail=f"Unknown bulk upload type '{entity}'")
    label = admin_bulk.ENTITIES[entity]["label"]
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail=f"Only district users can bulk upload {label} records")
    if not current_user.district_id:
        raise HTTPException(status_code=403, detail="Your account is not linked to a district")
    if not file.filename or not file.filename.lower().endswith((".csv", ".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only CSV or Excel (.csv, .xlsx, .xls) files are allowed")


@router.post("/bulk-upload/{entity}/validate", tags=["Bulk Upload"])
def bulk_upload_validate(
    entity: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """STEP 1 (dry run): validate the file and classify every row. Saves nothing.
    Row statuses: new | duplicate_update | duplicate_identical | file_duplicate | conflict | invalid"""
    _bulk_checks(entity, file, current_user)
    try:
        df = admin_bulk.read_upload_dataframe(entity, file.filename, file.file.read())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        results = admin_bulk.analyze_dataframe(db, entity, df, current_user)
        db.rollback()  # dry run: make sure nothing is left pending
        return {
            "entity": entity,
            "file_name": file.filename,
            "total_records": len(results),
            "summary": admin_bulk.summarize(results),
            "rows": admin_bulk.serialize_rows(results),
        }
    except Exception as e:
        db.rollback()
        logger.error(f"[ADMIN BULK VALIDATE] {entity} | {e} | {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Error validating file: {str(e)}")


def _run_admin_bulk_upload(request, entity, file, db, current_user, update_mode, selected_rows):
    _bulk_checks(entity, file, current_user)
    if update_mode not in admin_bulk.UPDATE_MODES:
        raise HTTPException(status_code=400,
                            detail=f"update_mode must be one of {', '.join(admin_bulk.UPDATE_MODES)}")
    try:
        df = admin_bulk.read_upload_dataframe(entity, file.filename, file.file.read())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    audit_name = admin_bulk.ENTITIES[entity]["audit"]
    bulk_upload = BulkUpload(file_name=f"[{audit_name}] {file.filename}"[:255], uploaded_by=current_user.id,
                             total_records=len(df), processing_status="processing")
    db.add(bulk_upload)
    db.commit()
    db.refresh(bulk_upload)
    logger.info(f"[ADMIN BULK] Started | {entity} | file='{file.filename}' | rows={len(df)} | user={current_user.id} | mode={update_mode}")

    try:
        # Re-analyse server-side: never trust the client's preview.
        results = admin_bulk.analyze_dataframe(db, entity, df, current_user)
        out = admin_bulk.execute_upload(db, entity, results, current_user, update_mode, selected_rows)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[ADMIN BULK] FAILED | {entity} | {e} | {traceback.format_exc()}")
        bulk_upload.processing_status = "failed"
        bulk_upload.error_log = f"{type(e).__name__}: {str(e)[:500]}"
        bulk_upload.completed_at = datetime.now()
        db.commit()
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")

    inserted, updated = out["inserted"], out["updated"]
    duplicate, failed = out["duplicate"], out["failed"]
    bulk_upload.successful_records = inserted + updated
    bulk_upload.failed_records = failed
    bulk_upload.duplicate_records = duplicate
    bulk_upload.processing_status = "completed"
    bulk_upload.error_log = "\n".join(out["errors"]) if out["errors"] else None
    bulk_upload.completed_at = datetime.now()
    db.commit()

    ip_address, user_agent = get_client_info(request)
    for rec_id, old_vals, new_vals in out["audit"]:
        log_audit(db, current_user.id, "BULK_UPDATE", audit_name, rec_id,
                  old_vals, {**new_vals, "bulk_upload_id": bulk_upload.id}, ip_address, user_agent)
    log_bulk_action(
        db, current_user.id, "BULK_UPLOAD", audit_name, inserted + updated,
        {"total_records": len(df), "inserted": inserted, "updated": updated, "failed": failed,
         "duplicate": duplicate, "update_mode": update_mode, "bulk_upload_id": bulk_upload.id},
        ip_address, user_agent
    )

    return {
        "message": "Bulk upload completed",
        "entity": entity,
        "total_records": len(df),
        "inserted": inserted,
        "updated": updated,
        "successful": inserted + updated,
        "failed": failed,
        "duplicate": duplicate,
        "bulk_upload_id": bulk_upload.id,
        "failed_rows": out["failed_rows"],
        # legacy keys used by the old one-step response
        "total": len(df),
        "errors": out["errors"],
    }


@router.post("/bulk-upload/{entity}/confirm", tags=["Bulk Upload"])
def bulk_upload_confirm(
    entity: str,
    request: Request,
    file: UploadFile = File(...),
    update_mode: str = Form("fill_empty"),
    selected_rows: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """STEP 2: insert new rows and, depending on update_mode, update existing ones.
      update_mode = skip        -> only insert new rows
                    fill_empty  -> also fill EMPTY fields on existing records (safe default)
                    overwrite   -> also overwrite fields whose value differs (Code is never changed)
      selected_rows = optional JSON list of Excel row numbers allowed to update existing records."""
    rows_filter = None
    if selected_rows:
        try:
            rows_filter = {int(x) for x in json.loads(selected_rows)}
        except Exception:
            raise HTTPException(status_code=400, detail="selected_rows must be a JSON list of row numbers")
    return _run_admin_bulk_upload(request, entity, file, db, current_user, update_mode, rows_filter)


@router.post("/bulk-upload/{entity}", tags=["Bulk Upload"])
def bulk_upload_legacy(
    entity: str,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Legacy one-step upload: inserts new rows, skips existing ones (no updates)."""
    return _run_admin_bulk_upload(request, entity, file, db, current_user, "skip", None)