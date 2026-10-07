from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, BackgroundTasks, Request
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import pandas as pd
import io
import logging
import traceback

logger = logging.getLogger(__name__)

from database import get_db
from models import PregnantWoman, User, BulkUpload, Notification, WardSubcentreMapping, BlockSubcentreMapping, District, Block, Ward, SubCentre
from schemas import (
    PregnantWomanCreate, PregnantWomanResponse, PregnantWomanUpdate,
    PaginatedResponse
)
from auth import get_current_active_user
# from auth import hash_aadhaar, verify_aadhaar, mask_aadhaar
from sms_service import sms_service
from sms_templates import get_sms_template
from audit_utils import (
    get_client_info,
    get_entity_snapshot,
    log_create,
    log_update,
    log_bulk_action
)
from services.notification_service import NotificationService
from sqlalchemy import func

router = APIRouter(prefix="/pregnant-women", tags=["Pregnant Women Management"])

@router.post("/", response_model=PregnantWomanResponse, status_code=status.HTTP_201_CREATED)
async def register_pregnant_woman(
    pw_data: PregnantWomanCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Register a new pregnant woman (by ANM/Sub-centre staff)
    """
    # Check for duplicates using ABHA ID or Mobile Number
    if pw_data.abha_id:
        existing_pw = db.query(PregnantWoman).filter(
            PregnantWoman.abha_id == pw_data.abha_id
        ).first()
        if existing_pw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Pregnant woman with this ABHA ID already exists"
            )
    
    # Aadhaar check disabled for current release
    # if pw_data.aadhaar_number:
    #     existing_pw = db.query(PregnantWoman).filter(
    #         PregnantWoman.aadhaar_number == hash_aadhaar(pw_data.aadhaar_number)
    #     ).first()
    #     if existing_pw:
    #         raise HTTPException(
    #             status_code=status.HTTP_400_BAD_REQUEST,
    #             detail="Pregnant woman with this Aadhaar number already exists"
    #         )
    
    existing_mobile = db.query(PregnantWoman).filter(
        PregnantWoman.mobile_number == pw_data.mobile_number
    ).first()
    if existing_mobile and existing_mobile.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pregnant woman with this mobile number already exists"
        )
    
    # Aadhaar data is currently disabled; keep any provided values out of storage
    data_dict = pw_data.dict()
    # if data_dict.get('aadhaar_number'):
    #     raw_aadhaar = data_dict['aadhaar_number']
    #     data_dict['aadhaar_number'] = hash_aadhaar(raw_aadhaar)
    #     data_dict['aadhaar_masked'] = mask_aadhaar(raw_aadhaar)
    # data_dict['aadhaar_number'] = None
    # data_dict['aadhaar_masked'] = None
    # Convert ward_id = 0 to None
    if data_dict.get('ward_id') == 0:
        data_dict['ward_id'] = None
    
    if not pw_data.sub_centre_id:
        # Priority 1: If ward_id provided, use ward-to-subcentre mapping
        if data_dict.get('ward_id'):
            ward_mapping = db.query(WardSubcentreMapping).filter(
                WardSubcentreMapping.ward_id == data_dict['ward_id']
            ).first()
            
            if ward_mapping:
                data_dict['sub_centre_id'] = ward_mapping.sub_centre_id
        
        # Priority 2: If no ward but block_id provided, use block-to-subcentre mapping
        elif pw_data.block_id:
            block_mapping = db.query(BlockSubcentreMapping).filter(
                BlockSubcentreMapping.block_id == pw_data.block_id
            ).first()
            
            if block_mapping:
                data_dict['sub_centre_id'] = block_mapping.sub_centre_id
    
    # Create new pregnant woman record
    new_pw = PregnantWoman(
        **data_dict,
        registered_by=current_user.id,
        registration_approved=True  # Auto-approved when registered by staff
    )
    
    db.add(new_pw)
    db.commit()
    db.refresh(new_pw)

    ip_address, user_agent = get_client_info(request)
    log_create(
        db,
        current_user.id,
        "PregnantWoman",
        new_pw.id,
        get_entity_snapshot(new_pw),
        ip_address,
        user_agent
    )
    
    # Send registration confirmation SMS
    _sms = get_sms_template("pregnancy_registration")
    background_tasks.add_task(
        sms_service.send_sms,
        mobile_number=new_pw.mobile_number,
        message=_sms["message"],
        message_type="pregnancy_registration",
        pregnant_woman_id=new_pw.id,
        db=db,
        template_id=_sms["template_id"]
    )
    
    return new_pw

@router.get("/", response_model=List[PregnantWomanResponse])
async def get_pregnant_women(
    skip: int = 0,
    limit: int = 100,
    district_id: Optional[int] = None,
    block_id: Optional[int] = None,
    sub_centre_id: Optional[int] = None,
    is_high_risk: Optional[bool] = None,
    is_active: Optional[bool] = True,
    registration_approved: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get list of pregnant women with filters
    """
    query = db.query(PregnantWoman)
    
    # Apply role-based filters
    if current_user.role == "district":
        query = query.filter(PregnantWoman.district_id == current_user.district_id)
    elif current_user.role == "block":
        query = query.filter(PregnantWoman.block_id == current_user.block_id)
    elif current_user.role == "sub_centre":
        # Check if user has specific ward assignments
        from models import UserWardMapping
        user_wards = db.query(UserWardMapping.ward_id).filter(
            UserWardMapping.user_id == current_user.id
        ).all()
        
        if user_wards:
            # User has specific ward assignments - filter by those wards
            ward_ids = [w.ward_id for w in user_wards]
            query = query.filter(PregnantWoman.ward_id.in_(ward_ids))
        else:
            # No specific ward assignments - show all in sub-centre (old behavior)
            from models import WardSubcentreMapping
            mapped_ward_ids = db.query(WardSubcentreMapping.ward_id).filter(
                WardSubcentreMapping.sub_centre_id == current_user.sub_centre_id
            ).subquery()
            
            query = query.filter(
                (PregnantWoman.ward_id.in_(mapped_ward_ids)) |
                (PregnantWoman.sub_centre_id == current_user.sub_centre_id)
            )
    
    # Additional filters
    if district_id:
        query = query.filter(PregnantWoman.district_id == district_id)
    if block_id:
        query = query.filter(PregnantWoman.block_id == block_id)
    if sub_centre_id:
        query = query.filter(PregnantWoman.sub_centre_id == sub_centre_id)
    if is_high_risk is not None:
        query = query.filter(PregnantWoman.is_high_risk == is_high_risk)
    if is_active is not None:
        query = query.filter(PregnantWoman.is_active == is_active)
    if registration_approved is not None:
        query = query.filter(PregnantWoman.registration_approved == registration_approved)
    
    pregnant_women = query.order_by(PregnantWoman.created_at.desc()).offset(skip).limit(limit).all()
    return pregnant_women

@router.get("/pending-approval", response_model=List[PregnantWomanResponse])
async def get_pending_approvals(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get self-registered pregnant women pending approval
    """
    query = db.query(PregnantWoman).filter(
        PregnantWoman.is_self_registered == True,
        PregnantWoman.registration_approved == False
    )
    
    # Filter based on user role
    if current_user.role == "district":
        query = query.filter(PregnantWoman.district_id == current_user.district_id)
    elif current_user.role == "block":
        query = query.filter(PregnantWoman.block_id == current_user.block_id)
    elif current_user.role == "sub_centre":
        # Check if user has specific ward assignments
        from models import UserWardMapping
        user_wards = db.query(UserWardMapping.ward_id).filter(
            UserWardMapping.user_id == current_user.id
        ).all()
        
        if user_wards:
            # User has specific ward assignments - filter by those wards
            ward_ids = [w.ward_id for w in user_wards]
            query = query.filter(PregnantWoman.ward_id.in_(ward_ids))
        else:
            # No specific ward assignments - show all in sub-centre (old behavior)
            from models import WardSubcentreMapping
            mapped_ward_ids = db.query(WardSubcentreMapping.ward_id).filter(
                WardSubcentreMapping.sub_centre_id == current_user.sub_centre_id
            ).subquery()
            
            query = query.filter(
                (PregnantWoman.ward_id.in_(mapped_ward_ids)) |
                (PregnantWoman.sub_centre_id == current_user.sub_centre_id)
            )
    
    return query.order_by(PregnantWoman.created_at.desc()).all()

@router.get("/{pw_id}", response_model=PregnantWomanResponse)
async def get_pregnant_woman(
    pw_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get pregnant woman details by ID
    """
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pw_id).first()
    if not pw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pregnant woman not found"
        )
    
    return pw

@router.put("/{pw_id}", response_model=PregnantWomanResponse)
async def update_pregnant_woman(
    pw_id: int,
    pw_update: PregnantWomanUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Update pregnant woman information
    """
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pw_id).first()
    if not pw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pregnant woman not found"
        )
    
    # Update fields
    old_values = get_entity_snapshot(pw)
    update_data = pw_update.dict(exclude_unset=True)
    
    # Capture high-risk flag before applying updates
    was_high_risk = pw.is_high_risk
    
    for field, value in update_data.items():
        setattr(pw, field, value)
    
    pw.updated_at = datetime.now()
    
    # Check if marked as high-risk and send SMS
    if pw_update.is_high_risk and not was_high_risk:
        # Send high-risk alert notification
        recipients = NotificationService.get_recipients_for_event(
            db,
            "high_risk_alert",
            {
                "district_id": pw.district_id,
                "block_id": pw.block_id,
                "sub_centre_id": pw.sub_centre_id
            }
        )
        
        if recipients:
            NotificationService.create_notification(
                db=db,
                user_ids=recipients,
                title="⚠️ High-Risk Case Identified",
                message=f"{pw.full_name} has been marked as high-risk. Immediate attention required.",
                notification_type="high_risk_alert",
                category="registration",
                priority="high",
                reference_id=pw.id,
                reference_type="pregnant_woman",
                action_url=f"/pregnant-women/{pw.id}",
                metadata={"pregnant_woman_name": pw.full_name, "risk_factors": pw.risk_factors}
            )
        
        _sms = get_sms_template("high_risk_alert")
        # Use background task if available, otherwise send directly
        try:
            await sms_service.send_sms(
                mobile_number=pw.mobile_number,
                message=_sms["message"],
                message_type="high_risk_alert",
                pregnant_woman_id=pw.id,
                db=db,
                template_id=_sms["template_id"]
            )
        except:
            pass  # SMS sending is optional
    
    db.commit()
    db.refresh(pw)

    ip_address, user_agent = get_client_info(request)
    log_update(
        db,
        current_user.id,
        "PregnantWoman",
        pw.id,
        old_values,
        get_entity_snapshot(pw),
        ip_address,
        user_agent
    )
    
    return pw

@router.post("/{pw_id}/approve")
async def approve_registration(
    pw_id: int,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Approve self-registered pregnant woman.
    Idempotent: calling again on an already-approved PW returns success without
    repeating side effects (SMS, notification, audit log).
    """
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pw_id).first()
    if not pw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pregnant woman not found"
        )

    if not pw.is_self_registered:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This record was not self-registered"
        )

    # Idempotency guard — already approved, return success without side effects
    if pw.registration_approved:
        return {"message": "Registration already approved", "already_approved": True}

    # Rejected PW cannot be approved — must be re-activated first
    if not pw.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot approve an inactive (rejected) registration. Please re-activate the record first."
        )

    old_values = get_entity_snapshot(pw)
    pw.registration_approved = True
    pw.registered_by = current_user.id
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_update(
        db,
        current_user.id,
        "PregnantWoman",
        pw.id,
        old_values,
        get_entity_snapshot(pw),
        ip_address,
        user_agent
    )

    NotificationService.create_notification(
        db=db,
        user_ids=[current_user.id],
        title="Registration Approved",
        message=f"Registration for {pw.full_name} has been approved",
        notification_type="registration_approved",
        category="registration",
        priority="normal",
        reference_id=pw.id,
        reference_type="pregnant_woman",
        action_url=f"/pregnant-women/{pw.id}",
        metadata={"pregnant_woman_name": pw.full_name}
    )

    _sms = get_sms_template("pregnancy_registration")
    background_tasks.add_task(
        sms_service.send_sms,
        mobile_number=pw.mobile_number,
        message=_sms["message"],
        message_type="pregnancy_registration",
        pregnant_woman_id=pw.id,
        db=db,
        template_id=_sms["template_id"]
    )

    return {"message": "Registration approved successfully", "already_approved": False}

@router.post("/{pw_id}/reject")
async def reject_registration(
    pw_id: int,
    rejection_reason: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Reject self-registered pregnant woman
    """
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pw_id).first()
    if not pw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pregnant woman not found"
        )
    
    if not pw.is_self_registered:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This record was not self-registered"
        )
    
    old_values = get_entity_snapshot(pw)
    pw.is_active = False  # Deactivate rejected registration
    db.commit()

    ip_address, user_agent = get_client_info(request)
    log_update(
        db,
        current_user.id,
        "PregnantWoman",
        pw.id,
        old_values,
        get_entity_snapshot(pw),
        ip_address,
        user_agent
    )
    
    # Send rejection notification
    NotificationService.create_notification(
        db=db,
        user_ids=[current_user.id],  # Notify the rejector
        title="Registration Rejected",
        message=f"Registration for {pw.full_name} has been rejected. Reason: {rejection_reason}",
        notification_type="registration_rejected",
        category="registration",
        priority="normal",
        reference_id=pw.id,
        reference_type="pregnant_woman",
        action_url=f"/pregnant-women/{pw.id}",
        metadata={"pregnant_woman_name": pw.full_name, "rejection_reason": rejection_reason}
    )
    
    return {"message": "Registration rejected successfully"}

# ==================== HELPER FUNCTIONS FOR BULK UPLOAD ====================

def _parse_date_field(value):
    """
    Safely convert Excel date value to Python date.
    Accepts: pandas Timestamp, datetime, date, or string (YYYY-MM-DD / DD-MM-YYYY / DD/MM/YYYY).
    Returns: datetime.date or None if empty.
    Raises: ValueError if value is present but unparseable.
    """
    import pandas as pd
    from datetime import date, datetime

    if value is None or (hasattr(pd, 'isna') and pd.isna(value)):
        return None
    # pandas Timestamp or datetime
    if hasattr(value, 'date'):
        return value.date() if callable(value.date) else value.date
    if isinstance(value, date):
        return value
    # String fallback
    s = str(value).strip()
    if not s or s.lower() in ('nan', 'nat', 'none', ''):
        return None
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%m/%d/%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date format: '{s}'. Expected YYYY-MM-DD or DD-MM-YYYY.")


def _parse_int_field(value, field_name):
    """
    Safely convert Excel numeric value to int.
    Accepts: int, float (e.g. 2.0 from Excel), or numeric string.
    Returns: int or None if empty.
    Raises: ValueError if value is present but not a valid integer.
    """
    import pandas as pd

    if value is None or (hasattr(pd, 'isna') and pd.isna(value)):
        return None
    s = str(value).strip()
    if not s or s.lower() in ('nan', 'none', ''):
        return None
    try:
        f = float(s)
        if not f.is_integer():
            raise ValueError(f"'{field_name}' must be a whole number, got: '{s}'")
        return int(f)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid value for '{field_name}': '{s}'. Must be a whole number.")

def lookup_entity_by_name(db: Session, entity_model, name: str, filter_conditions: dict = None):
    """
    Generic function to lookup entity by name (case-insensitive)
    Returns entity or None
    """
    if not name or pd.isna(name):
        return None
    
    # Remove extra whitespace and make case-insensitive comparison
    name = str(name).strip()
    
    query = db.query(entity_model).filter(
        func.lower(entity_model.name) == func.lower(name)
    )
    
    # Add additional filter conditions if provided
    if filter_conditions:
        for field, value in filter_conditions.items():
            query = query.filter(getattr(entity_model, field) == value)
    
    return query.first()


def resolve_administrative_ids(db: Session, row: pd.Series, current_user: User):
    """
    Resolve administrative names to IDs
    Returns dict with resolved IDs and any errors
    """
    result = {
        "district_id": None,
        "block_id": None,
        "ward_id": None,
        "sub_centre_id": None,
        "errors": []
    }
    
    # 1. DISTRICT - Resolve from name or use current user's district
    if pd.notna(row.get('district_name')):
        district_name = str(row.get('district_name')).strip()
        district = lookup_entity_by_name(db, District, district_name)
        
        if district:
            result["district_id"] = district.id
            
            # Validate district matches user's district (if user has district_id)
            if current_user.district_id and district.id != current_user.district_id:
                result["errors"].append(f"District '{district_name}' does not match your assigned district")
        else:
            result["errors"].append(f"District '{district_name}' not found in system")
    
    # Fallback: Use district_id if provided (backward compatibility)
    elif pd.notna(row.get('district_id')):
        result["district_id"] = int(row.get('district_id'))
    
    # Default: Use current user's district
    else:
        result["district_id"] = current_user.district_id
    
    
    # 2. BLOCK - Resolve from name
    if pd.notna(row.get('block_name')):
        block_name = str(row.get('block_name')).strip()
        
        # Lookup block within the district
        block = lookup_entity_by_name(
            db, Block, block_name,
            {"district_id": result["district_id"]} if result["district_id"] else None
        )
        
        if block:
            result["block_id"] = block.id
            
            # Validate block matches user's block (if user is block user)
            if current_user.role == "block" and block.id != current_user.block_id:
                result["errors"].append(f"Block '{block_name}' does not match your assigned block")
        else:
            result["errors"].append(f"Block '{block_name}' not found in district")
    
    # Fallback: Use block_id if provided (backward compatibility)
    elif pd.notna(row.get('block_id')):
        result["block_id"] = int(row.get('block_id'))
    
    # Default: Use current user's block
    elif current_user.role == "block":
        result["block_id"] = current_user.block_id
    
    
    # 3. WARD - Resolve from name
    if pd.notna(row.get('ward_name')):
        ward_name = str(row.get('ward_name')).strip()
        
        # Lookup ward within the block
        ward = lookup_entity_by_name(
            db, Ward, ward_name,
            {"block_id": result["block_id"]} if result["block_id"] else None
        )
        
        if ward:
            result["ward_id"] = ward.id
        else:
            result["errors"].append(f"Ward '{ward_name}' not found in block")
    
    # Fallback: Use ward_id if provided (backward compatibility)
    elif pd.notna(row.get('ward_id')):
        ward_id = row.get('ward_id')
        if ward_id != 0:  # 0 means no ward
            result["ward_id"] = int(ward_id)
    
    
    # 4. SUB-CENTRE - Resolve from name (optional, can be auto-assigned)
    if pd.notna(row.get('sub_centre_name')):
        sub_centre_name = str(row.get('sub_centre_name')).strip()
        
        # Lookup sub-centre within the block
        sub_centre = lookup_entity_by_name(
            db, SubCentre, sub_centre_name,
            {"block_id": result["block_id"]} if result["block_id"] else None
        )
        
        if sub_centre:
            result["sub_centre_id"] = sub_centre.id
        else:
            result["errors"].append(f"Sub-Centre '{sub_centre_name}' not found in block")
    
    # Fallback: Use sub_centre_id if provided (backward compatibility)
    elif pd.notna(row.get('sub_centre_id')):
        sub_centre_id = row.get('sub_centre_id')
        if sub_centre_id:  # Only if not empty
            result["sub_centre_id"] = int(sub_centre_id)
    
    return result

# ==================== END HELPER FUNCTIONS ====================

@router.post("/bulk-upload")
async def bulk_upload_pregnant_women(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Bulk upload pregnant women data from Excel file (Block user only)
    """
    if current_user.role not in ["block", "district"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Block or District users can perform bulk upload"
        )
    
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only Excel files are allowed"
        )
    
    try:
        # Read Excel file
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        
        # ==================== REVERSE COLUMN MAPPING ====================
        # Handle human-readable headers from new template format
        # Maps "Full Name" → "full_name" for backward compatibility
        reverse_mapping = {
            "Full Name": "full_name",
            "Mobile Number": "mobile_number",
            # "Aadhaar Number": "aadhaar_number",
            "ABHA ID": "abha_id",
            "RCH ID": "rch_id",
            "Husband Name": "husband_name",
            "Age": "age",
            "District Name": "district_name",
            "Block Name": "block_name",
            "Ward Name": "ward_name",
            "Sub Centre Name": "sub_centre_name",
            "Date of Birth": "date_of_birth",
            "LMP Date": "lmp_date",
            "EDD Date": "edd_date",
            "Gravida": "gravida",
            "Para": "para",
            "Blood Group": "blood_group",
            "Address": "address",
            "HPR ID": "hpr_id"
        }
        
        # Rename columns if human-readable headers are present
        df = df.rename(columns=reverse_mapping)
        # ==================== END REVERSE MAPPING ====================
        
        # Create bulk upload record
        bulk_upload = BulkUpload(
            file_name=file.filename,
            uploaded_by=current_user.id,
            total_records=len(df),
            processing_status="processing"
        )
        db.add(bulk_upload)
        db.commit()
        db.refresh(bulk_upload)

        logger.info(f"[BULK UPLOAD] Started | file='{file.filename}' | total_rows={len(df)} | uploaded_by=user_id:{current_user.id} | columns={list(df.columns)}")

        successful = 0
        failed = 0
        duplicate = 0
        errors = []
        failed_rows = []  # structured list for response

        for index, row in df.iterrows():
            excel_row = index + 2  # Excel row number (1=header, so data starts at 2)
            try:
                # --- LOG: Row start ---
                logger.info(
                    f"[BULK UPLOAD] Processing Row {excel_row} | "
                    f"name='{row.get('full_name')}' | "
                    f"mobile='{row.get('mobile_number')}' | "
                    f"abha='{row.get('abha_id') if pd.notna(row.get('abha_id')) else None}' | "
                    f"district='{row.get('district_name') or row.get('district_id')}' | "
                    f"block='{row.get('block_name') or row.get('block_id')}' | "
                    f"ward='{row.get('ward_name') or row.get('ward_id')}'"
                )

                # --- STEP 1: Duplicate check ---
                existing_pw = None
                if pd.notna(row.get('abha_id')):
                    existing_pw = db.query(PregnantWoman).filter(
                        PregnantWoman.abha_id == row['abha_id']
                    ).first()
                    if existing_pw:
                        logger.warning(f"[BULK UPLOAD] Row {excel_row} DUPLICATE | matched_on=ABHA_ID | abha_id='{row['abha_id']}'")

                # Aadhaar duplicate matching disabled for current release
                # if not existing_pw and pd.notna(row.get('aadhaar_number')):
                #     existing_pw = db.query(PregnantWoman).filter(
                #         PregnantWoman.aadhaar_number == hash_aadhaar(str(row['aadhaar_number']).strip())
                #     ).first()
                #     if existing_pw:
                #         logger.warning(f"[BULK UPLOAD] Row {excel_row} DUPLICATE | matched_on=AADHAAR (masked)")

                if not existing_pw and pd.notna(row.get('mobile_number')):
                    existing_pw = db.query(PregnantWoman).filter(
                        PregnantWoman.mobile_number == row['mobile_number']
                    ).first()
                    if existing_pw:
                        logger.warning(f"[BULK UPLOAD] Row {excel_row} DUPLICATE | matched_on=MOBILE | mobile='{row['mobile_number']}'")

                if existing_pw:
                    duplicate += 1
                    errors.append(f"Row {excel_row} | Name: {row.get('full_name')} | Mobile: {row.get('mobile_number')} | Reason: Duplicate record (ABHA/Mobile already exists)")
                    failed_rows.append({"row_number": excel_row, "full_name": str(row.get('full_name') or ''), "mobile_number": str(row.get('mobile_number') or ''), "error_reason": "Already registered (duplicate ABHA / Mobile)"})
                    continue

                # --- STEP 2: Administrative ID resolution ---
                logger.info(f"[BULK UPLOAD] Row {excel_row} | Resolving administrative IDs...")
                resolved = resolve_administrative_ids(db, row, current_user)

                logger.info(
                    f"[BULK UPLOAD] Row {excel_row} | Resolved IDs: "
                    f"district_id={resolved['district_id']} | "
                    f"block_id={resolved['block_id']} | "
                    f"ward_id={resolved['ward_id']} | "
                    f"sub_centre_id={resolved['sub_centre_id']}"
                )

                if resolved["errors"]:
                    failed += 1
                    reason = ', '.join(resolved['errors'])
                    logger.warning(f"[BULK UPLOAD] Row {excel_row} FAILED | admin_resolution_errors='{reason}' | name='{row.get('full_name')}' | mobile='{row.get('mobile_number')}'")
                    errors.append(f"Row {excel_row} | Name: {row.get('full_name')} | Mobile: {row.get('mobile_number')} | Reason: {reason}")
                    failed_rows.append({"row_number": excel_row, "full_name": str(row.get('full_name') or ''), "mobile_number": str(row.get('mobile_number') or ''), "error_reason": reason})
                    continue

                # --- STEP 3: Required field validation ---
                if not resolved["district_id"]:
                    failed += 1
                    logger.warning(f"[BULK UPLOAD] Row {excel_row} FAILED | reason=District missing | name='{row.get('full_name')}' | mobile='{row.get('mobile_number')}'")
                    errors.append(f"Row {excel_row} | Name: {row.get('full_name')} | Mobile: {row.get('mobile_number')} | Reason: District is required")
                    failed_rows.append({"row_number": excel_row, "full_name": str(row.get('full_name') or ''), "mobile_number": str(row.get('mobile_number') or ''), "error_reason": "District is required but not provided or not found"})
                    continue

                if not resolved["block_id"]:
                    failed += 1
                    logger.warning(f"[BULK UPLOAD] Row {excel_row} FAILED | reason=Block missing | name='{row.get('full_name')}' | mobile='{row.get('mobile_number')}'")
                    errors.append(f"Row {excel_row} | Name: {row.get('full_name')} | Mobile: {row.get('mobile_number')} | Reason: Block is required")
                    failed_rows.append({"row_number": excel_row, "full_name": str(row.get('full_name') or ''), "mobile_number": str(row.get('mobile_number') or ''), "error_reason": "Block is required but not provided or not found"})
                    continue

                # --- STEP 4: Sub-centre auto-assignment ---
                ward_id = resolved["ward_id"]
                sub_centre_id = resolved["sub_centre_id"]
                block_id = resolved["block_id"]
                district_id = resolved["district_id"]

                if not sub_centre_id:
                    if ward_id:
                        ward_mapping = db.query(WardSubcentreMapping).filter(
                            WardSubcentreMapping.ward_id == ward_id
                        ).first()
                        if ward_mapping:
                            sub_centre_id = ward_mapping.sub_centre_id
                            logger.info(f"[BULK UPLOAD] Row {excel_row} | sub_centre assigned from WARD mapping | sub_centre_id={sub_centre_id}")

                    if not sub_centre_id and block_id:
                        block_mapping = db.query(BlockSubcentreMapping).filter(
                            BlockSubcentreMapping.block_id == block_id
                        ).first()
                        if block_mapping:
                            sub_centre_id = block_mapping.sub_centre_id
                            logger.info(f"[BULK UPLOAD] Row {excel_row} | sub_centre assigned from BLOCK mapping | sub_centre_id={sub_centre_id}")

                    if not sub_centre_id:
                        logger.info(f"[BULK UPLOAD] Row {excel_row} | sub_centre_id=None (no mapping found, allowed)")

                # --- STEP 5: Create record ---
                # raw_aadhaar = row.get('aadhaar_number') if pd.notna(row.get('aadhaar_number')) else None

                # Parse date and numeric fields — raises ValueError on bad data → row marked failed
                dob    = _parse_date_field(row.get('date_of_birth'))
                lmp    = _parse_date_field(row.get('lmp_date'))
                edd    = _parse_date_field(row.get('edd_date'))
                gravida_val = _parse_int_field(row.get('gravida'), 'gravida')
                para_val    = _parse_int_field(row.get('para'), 'para')

                new_pw = PregnantWoman(
                    full_name=row.get('full_name'),
                    mobile_number=row.get('mobile_number'),
                    abha_id=row.get('abha_id') if pd.notna(row.get('abha_id')) else None,
                    rch_id=row.get('rch_id') if pd.notna(row.get('rch_id')) else None,
                    # aadhaar_number=None,
                    # aadhaar_masked=None,
                    husband_name=row.get('husband_name') if pd.notna(row.get('husband_name')) else None,
                    age=int(row.get('age')) if pd.notna(row.get('age')) else None,
                    date_of_birth=dob,
                    lmp_date=lmp,
                    edd_date=edd,
                    gravida=gravida_val,
                    para=para_val,
                    address=row.get('address') if pd.notna(row.get('address')) else None,
                    ward_id=ward_id,
                    sub_centre_id=sub_centre_id,
                    block_id=block_id,
                    district_id=district_id,
                    hpr_id=row.get('hpr_id') if pd.notna(row.get('hpr_id')) else None,
                    blood_group=row.get('blood_group') if pd.notna(row.get('blood_group')) else None,
                    registered_by=current_user.id,
                    registration_approved=True
                )

                db.add(new_pw)
                successful += 1
                logger.info(f"[BULK UPLOAD] Row {excel_row} SUCCESS | name='{row.get('full_name')}' | mobile='{row.get('mobile_number')}'")

            except Exception as e:
                failed += 1
                error_detail = f"Row {excel_row} | Name: {row.get('full_name')} | Mobile: {row.get('mobile_number')} | Error: {type(e).__name__}: {str(e)}"
                errors.append(error_detail)
                # Clean user-friendly message — no stack trace exposed
                friendly_msg = str(e) if len(str(e)) < 120 else str(e)[:120] + "..."
                failed_rows.append({"row_number": excel_row, "full_name": str(row.get('full_name') or ''), "mobile_number": str(row.get('mobile_number') or ''), "error_reason": friendly_msg})
                logger.error(
                    f"[BULK UPLOAD] Row {excel_row} EXCEPTION | "
                    f"name='{row.get('full_name')}' | "
                    f"mobile='{row.get('mobile_number')}' | "
                    f"error_type={type(e).__name__} | "
                    f"error='{str(e)}' | "
                    f"traceback={traceback.format_exc()}"
                )

        # --- Final summary log ---
        logger.info(
            f"[BULK UPLOAD] Completed | file='{file.filename}' | "
            f"total={len(df)} | successful={successful} | failed={failed} | duplicate={duplicate}"
        )

        # Commit all records
        db.commit()
        
        # Update bulk upload record
        bulk_upload.successful_records = successful
        bulk_upload.failed_records = failed
        bulk_upload.duplicate_records = duplicate
        bulk_upload.processing_status = "completed"
        bulk_upload.error_log = "\n".join(errors) if errors else None
        bulk_upload.completed_at = datetime.now()
        db.commit()
        
        # Send notification to uploader
        NotificationService.create_notification(
            db=db,
            user_ids=[current_user.id],
            title="Bulk Upload Completed",
            message=f"Bulk upload completed: {successful} successful, {failed} failed, {duplicate} duplicates",
            notification_type="bulk_upload_completed",
            category="admin",
            priority="normal",
            reference_id=bulk_upload.id,
            reference_type="bulk_upload",
            action_url=f"/bulk-uploads/{bulk_upload.id}",
            metadata={
                "total": len(df),
                "successful": successful,
                "failed": failed,
                "duplicate": duplicate
            }
        )
        
        ip_address, user_agent = get_client_info(request)
        log_bulk_action(
            db,
            current_user.id,
            "BULK_UPLOAD",
            "PregnantWoman",
            successful,
            {
                "total_records": len(df),
                "successful": successful,
                "failed": failed,
                "duplicate": duplicate,
                "bulk_upload_id": bulk_upload.id
            },
            ip_address,
            user_agent
        )

        return {
            "message": "Bulk upload completed",
            "total_records": len(df),
            "successful": successful,
            "failed": failed,
            "duplicate": duplicate,
            "bulk_upload_id": bulk_upload.id,
            "failed_rows": failed_rows
        }
        
    except Exception as e:
        logger.error(f"[BULK UPLOAD] FILE-LEVEL ERROR | file='{file.filename}' | error_type={type(e).__name__} | error='{str(e)}' | traceback={traceback.format_exc()}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing file: {str(e)}"
        )

@router.get("/search/by-mobile/{mobile_number}", response_model=PregnantWomanResponse)
async def search_by_mobile(
    mobile_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Search pregnant woman by mobile number
    """
    pw = db.query(PregnantWoman).filter(
        PregnantWoman.mobile_number == mobile_number
    ).first()
    
    if not pw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pregnant woman not found"
        )
    
    return pw
