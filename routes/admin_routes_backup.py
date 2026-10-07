from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime

from database import get_db
from models import District, Block, Ward, SubCentre, USGCentre, WardSubcentreMapping, BlockSubcentreMapping, User, PregnantWoman, USGAppointment
from schemas import (
    DistrictCreate, DistrictUpdate, DistrictResponse,
    BlockCreate, BlockUpdate, BlockResponse,
    WardCreate, WardUpdate, WardResponse,
    SubCentreCreate, SubCentreUpdate, SubCentreResponse,
    USGCentreCreate, USGCentreUpdate, USGCentreResponse,
    WardMappingCreate, WardMappingResponse, BlockMappingCreate, BlockMappingResponse
)
from auth import get_current_active_user, get_district_user

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
    
    update_data = district_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(district, field, value)
    
    district.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(district)
    return district

@router.delete("/districts/{district_id}", tags=["District Management"])
async def delete_district(
    district_id: int,
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
    
    district.is_active = False
    district.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "District deactivated successfully"}

@router.post("/districts/{district_id}/activate", tags=["District Management"])
async def activate_district(
    district_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_district_user)
):
    """Reactivate district (District admin only)"""
    district = db.query(District).filter(District.id == district_id).first()
    if not district:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="District not found")
    
    district.is_active = True
    district.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "District activated successfully"}

# BLOCK MANAGEMENT
@router.post("/blocks", response_model=BlockResponse, status_code=status.HTTP_201_CREATED, tags=["Block Management"])
async def create_block(
    block_data: BlockCreate,
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
    if current_user.role == "district" and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and block.id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    return block

@router.put("/blocks/{block_id}", response_model=BlockResponse, tags=["Block Management"])
async def update_block(
    block_id: int,
    block_update: BlockUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Update block"""
    block = db.query(Block).filter(Block.id == block_id).first()
    if not block:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")
    
    # Permission check
    if current_user.role == "district" and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and block.id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Check code uniqueness if code is being updated
    if block_update.code and block_update.code != block.code:
        if not check_code_uniqueness(db, Block, block_update.code, district_id=block.district_id, exclude_id=block_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Block code already exists in this district"
            )
    
    update_data = block_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(block, field, value)
    
    block.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(block)
    return block

@router.delete("/blocks/{block_id}", tags=["Block Management"])
async def delete_block(
    block_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Deactivate block"""
    block = db.query(Block).filter(Block.id == block_id).first()
    if not block:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")
    
    # Permission check
    if current_user.role == "district" and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role not in ["district"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Check dependencies
    dependencies = check_entity_dependencies(db, "block", block_id)
    if not dependencies["can_delete"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete block. Dependencies: {', '.join(dependencies['dependencies'])}"
        )
    
    block.is_active = False
    block.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "Block deactivated successfully"}

# Continue with Ward, SubCentre, and USG Centre management...
# (This is getting long, so I'll create the rest in the next part)

# WARD MANAGEMENT
@router.post("/wards", response_model=WardResponse, status_code=status.HTTP_201_CREATED, tags=["Ward Management"])
async def create_ward(
    ward_data: WardCreate,
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
    if current_user.role == "district" and block.district_id != current_user.district_id:
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Update ward"""
    ward = db.query(Ward).filter(Ward.id == ward_id).first()
    if not ward:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ward not found")
    
    # Permission check
    block = db.query(Block).filter(Block.id == ward.block_id).first()
    if current_user.role == "district" and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and ward.block_id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    update_data = ward_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(ward, field, value)
    
    db.commit()
    db.refresh(ward)
    return ward

@router.delete("/wards/{ward_id}", tags=["Ward Management"])
async def delete_ward(
    ward_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Deactivate ward"""
    ward = db.query(Ward).filter(Ward.id == ward_id).first()
    if not ward:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ward not found")
    
    # Permission check
    block = db.query(Block).filter(Block.id == ward.block_id).first()
    if current_user.role == "district" and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and ward.block_id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    # Check dependencies
    dependencies = check_entity_dependencies(db, "ward", ward_id)
    if not dependencies["can_delete"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete ward. Dependencies: {', '.join(dependencies['dependencies'])}"
        )
    
    ward.is_active = False
    db.commit()
    return {"message": "Ward deactivated successfully"}

# SUB-CENTRE MANAGEMENT
@router.post("/sub-centres", response_model=SubCentreResponse, status_code=status.HTTP_201_CREATED, tags=["Sub-Centre Management"])
async def create_sub_centre(
    subcentre_data: SubCentreCreate,
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
    if current_user.role == "district" and block.district_id != current_user.district_id:
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Update sub-centre"""
    subcentre = db.query(SubCentre).filter(SubCentre.id == subcentre_id).first()
    if not subcentre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-centre not found")
    
    # Permission check
    block = db.query(Block).filter(Block.id == subcentre.block_id).first()
    if current_user.role == "district" and block.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "block" and subcentre.block_id != current_user.block_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "sub_centre" and subcentre.id != current_user.sub_centre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    update_data = subcentre_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(subcentre, field, value)
    
    subcentre.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(subcentre)
    return subcentre

@router.delete("/sub-centres/{subcentre_id}", tags=["Sub-Centre Management"])
async def delete_sub_centre(
    subcentre_id: int,
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
    
    subcentre.is_active = False
    subcentre.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "Sub-centre deactivated successfully"}

# USG CENTRE MANAGEMENT
@router.post("/usg-centres", response_model=USGCentreResponse, status_code=status.HTTP_201_CREATED, tags=["USG Centre Management"])
async def create_usg_centre(
    usgcentre_data: USGCentreCreate,
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
    
    usgcentre = USGCentre(**usgcentre_data.dict())
    db.add(usgcentre)
    db.commit()
    db.refresh(usgcentre)
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
        query = query.filter(USGCentre.block_id == block_id)
    if ward_id:
        query = query.filter(USGCentre.ward_id == ward_id)
    if is_active is not None:
        query = query.filter(USGCentre.is_active == is_active)
    if is_private is not None:
        query = query.filter(USGCentre.is_private == is_private)
    
    return query.offset(skip).limit(limit).all()

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
    return usgcentre

@router.put("/usg-centres/{usgcentre_id}", response_model=USGCentreResponse, tags=["USG Centre Management"])
async def update_usg_centre(
    usgcentre_id: int,
    usgcentre_update: USGCentreUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Update USG centre"""
    usgcentre = db.query(USGCentre).filter(USGCentre.id == usgcentre_id).first()
    if not usgcentre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="USG centre not found")
    
    # Permission check
    if current_user.role == "district" and usgcentre.district_id != current_user.district_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role == "usg_centre" and usgcentre.id != current_user.usg_centre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    elif current_user.role not in ["district", "usg_centre"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
    update_data = usgcentre_update.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(usgcentre, field, value)
    
    usgcentre.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(usgcentre)
    return usgcentre

@router.delete("/usg-centres/{usgcentre_id}", tags=["USG Centre Management"])
async def delete_usg_centre(
    usgcentre_id: int,
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
    
    usgcentre.is_active = False
    usgcentre.updated_at = datetime.utcnow()
    db.commit()
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
    
    return mappings

@router.delete("/sub-centres/{subcentre_id}/wards/{ward_id}", tags=["Ward-SubCentre Mapping"])
async def remove_ward_mapping(
    subcentre_id: int,
    ward_id: int,
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
    
    db.delete(mapping)
    db.commit()
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
    
    return mappings

@router.delete("/sub-centres/{subcentre_id}/blocks/{block_id}", tags=["Block-SubCentre Mapping"])
async def remove_block_mapping(
    subcentre_id: int,
    block_id: int,
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
    
    db.delete(mapping)
    db.commit()
    return {"message": "Block mapping removed successfully"}