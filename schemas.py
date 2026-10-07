from pydantic import BaseModel, EmailStr, Field, validator
from typing import Optional, List
from datetime import datetime, date
from enum import Enum

# Enums
class UserRoleEnum(str, Enum):
    DISTRICT = "district"
    BLOCK = "block"
    SUB_CENTRE = "sub_centre"
    USG_CENTRE = "usg_centre"
    DP = "dp"
    PMSMA = "pmsma"

class GrievanceStatusEnum(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    ESCALATED = "escalated"

class AppointmentStatusEnum(str, Enum):
    SCHEDULED = "scheduled"
    ACCEPTED = "accepted"
    RESCHEDULED = "rescheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class AppointmentTypeEnum(str, Enum):
    REGULAR = "regular"
    EMERGENCY = "emergency"

class ReferralStatusEnum(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    RE_REFERRED = "re_referred"
    COMPLETED = "completed"

class DeliveryTypeEnum(str, Enum):
    SAFE_DELIVERY = "safe_delivery"
    LIVE_BIRTH = "live_birth"
    STILL_BIRTH = "still_birth"
    INFANT_DEATH = "infant_death"
    MATERNAL_DEATH = "maternal_death"

class BabyGenderEnum(str, Enum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"

class MaternalOutcomeEnum(str, Enum):
    HEALTHY = "healthy"
    COMPLICATIONS = "complications"
    REFERRED_HIGHER_FACILITY = "referred_higher_facility"
    MATERNAL_DEATH = "maternal_death"

# Base Schemas
class BaseSchema(BaseModel):
    class Config:
        from_attributes = True

# User Schemas
class UserBase(BaseSchema):
    username: str
    email: Optional[EmailStr] = None
    role: UserRoleEnum
    full_name: str
    mobile_number: str
    district_id: Optional[int] = None
    block_id: Optional[int] = None
    sub_centre_id: Optional[int] = None
    usg_centre_id: Optional[int] = None
    dp_id: Optional[int] = None
    pmsma_centre_id: Optional[int] = None

class UserCreate(UserBase):
    password: str
    ward_ids: Optional[List[int]] = None  # For sub-centre users to assign specific wards
    
    @validator('password')
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters long')
        if not any(c.isupper() for c in v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not any(c.islower() for c in v):
            raise ValueError('Password must contain at least one lowercase letter')
        if not any(c.isdigit() for c in v):
            raise ValueError('Password must contain at least one number')
        if not any(c in '!@#$%^&*()_+-=[]{}|;:,.<>?' for c in v):
            raise ValueError('Password must contain at least one special character')
        return v

class UserUpdate(BaseSchema):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    mobile_number: Optional[str] = None
    is_active: Optional[bool] = None

class UserResponse(UserBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
    ward_ids: Optional[List[int]] = None  # For sub-centre users

class UserLogin(BaseSchema):
    username: str
    password: str

class Token(BaseSchema):
    access_token: str
    token_type: str

class TokenData(BaseSchema):
    username: Optional[str] = None

# Password Reset Schemas
class ForgotPasswordRequest(BaseSchema):
    identifier: str  # username or email

class ResetPasswordRequest(BaseSchema):
    identifier: str  # username or email
    otp: str
    new_password: str
    
    @validator('new_password')
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters long')
        if not any(c.isupper() for c in v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not any(c.islower() for c in v):
            raise ValueError('Password must contain at least one lowercase letter')
        if not any(c.isdigit() for c in v):
            raise ValueError('Password must contain at least one number')
        if not any(c in '!@#$%^&*()_+-=[]{}|;:,.<>?' for c in v):
            raise ValueError('Password must contain at least one special character')
        return v

# District Schemas
class DistrictBase(BaseSchema):
    name: str
    code: str

class DistrictCreate(DistrictBase):
    pass

class DistrictUpdate(BaseSchema):
    name: Optional[str] = None
    code: Optional[str] = None
    is_active: Optional[bool] = None

class DistrictResponse(DistrictBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

# Block Schemas
class BlockBase(BaseSchema):
    name: str
    name_regional: Optional[str] = None
    code: str
    district_id: int

class BlockCreate(BlockBase):
    pass

class BlockUpdate(BaseSchema):
    name: Optional[str] = None
    name_regional: Optional[str] = None
    code: Optional[str] = None
    district_id: Optional[int] = None
    is_active: Optional[bool] = None

class BlockResponse(BlockBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

# Ward Schemas
class WardBase(BaseSchema):
    name: str
    name_regional: Optional[str] = None
    code: str
    block_id: int

class WardCreate(WardBase):
    pass

class WardUpdate(BaseSchema):
    name: Optional[str] = None
    name_regional: Optional[str] = None
    code: Optional[str] = None
    block_id: Optional[int] = None
    is_active: Optional[bool] = None

class WardResponse(WardBase):
    id: int
    is_active: bool
    created_at: datetime

# SubCentre Schemas
class SubCentreBase(BaseSchema):
    name: str
    code: str
    block_id: int
    address: Optional[str] = None
    contact_number: Optional[str] = None

class SubCentreCreate(SubCentreBase):
    pass

class SubCentreUpdate(BaseSchema):
    name: Optional[str] = None
    code: Optional[str] = None
    block_id: Optional[int] = None
    address: Optional[str] = None
    contact_number: Optional[str] = None
    is_active: Optional[bool] = None

class SubCentreResponse(SubCentreBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

# USG Centre Schemas
class USGCentreBase(BaseSchema):
    name: str
    code: str
    address: Optional[str] = None
    contact_number: Optional[str] = None
    contact_person_name: Optional[str] = None
    email: Optional[EmailStr] = None
    google_map_url: Optional[str] = None
    is_empanelled: bool = True
    is_private: bool = False
    district_id: Optional[int] = None
    block_id: Optional[int] = None
    ward_id: Optional[int] = None

class USGCentreCreate(USGCentreBase):
    block_ids: Optional[List[int]] = None  # Multiple blocks support

class USGCentreUpdate(BaseSchema):
    name: Optional[str] = None
    code: Optional[str] = None
    address: Optional[str] = None
    contact_number: Optional[str] = None
    contact_person_name: Optional[str] = None
    email: Optional[EmailStr] = None
    google_map_url: Optional[str] = None
    is_empanelled: Optional[bool] = None
    is_private: Optional[bool] = None
    district_id: Optional[int] = None
    block_id: Optional[int] = None
    ward_id: Optional[int] = None
    is_active: Optional[bool] = None
    block_ids: Optional[List[int]] = None  # Multiple blocks support

class USGCentreResponse(USGCentreBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
    block_ids: Optional[List[int]] = None  # Multiple blocks support

# Ward-SubCentre Mapping Schemas
class WardMappingCreate(BaseSchema):
    ward_ids: List[int]

class WardMappingResponse(BaseSchema):
    id: int
    ward_id: int
    sub_centre_id: int
    created_at: datetime

# Block-SubCentre Mapping Schemas
class BlockMappingCreate(BaseSchema):
    block_ids: List[int]

class BlockMappingResponse(BaseSchema):
    id: int
    block_id: int
    sub_centre_id: int
    created_at: datetime

# Pregnant Woman Schemas
class PregnantWomanBase(BaseSchema):
    full_name: str
    mobile_number: str
    abha_id: Optional[str] = None
    rch_id: Optional[str] = None
    # aadhaar_number: Optional[str] = None  # input only — plain text from frontend
    husband_name: Optional[str] = None
    date_of_birth: Optional[date] = None
    age: Optional[int] = None
    address: Optional[str] = None
    ward_id: Optional[int] = None
    block_id: Optional[int] = None
    district_id: Optional[int] = None
    sub_centre_id: Optional[int] = None
    lmp_date: Optional[date] = None
    edd_date: Optional[date] = None
    gravida: Optional[int] = None
    para: Optional[int] = None
    hpr_id: Optional[str] = None
    blood_group: Optional[str] = None
    is_high_risk: bool = False
    risk_factors: Optional[str] = None

class PregnantWomanCreate(PregnantWomanBase):
    is_self_registered: bool = False

class PregnantWomanSelfRegister(BaseSchema):
    full_name: str
    mobile_number: str
    ward_id: Optional[int] = None
    block_id: int
    district_id: int
    husband_name: Optional[str] = None
    # aadhaar_number: Optional[str] = None
    date_of_birth: Optional[date] = None
    age: Optional[int] = None
    full_address: Optional[str] = None

class PregnantWomanUpdate(BaseSchema):
    full_name: Optional[str] = None
    husband_name: Optional[str] = None
    mobile_number: Optional[str] = None
    address: Optional[str] = None
    blood_group: Optional[str] = None
    is_high_risk: Optional[bool] = None
    risk_factors: Optional[str] = None
    lmp_date: Optional[date] = None
    date_of_birth: Optional[date] = None
    age: Optional[int] = None
    gravida: Optional[int] = None
    para: Optional[int] = None
    abha_id: Optional[str] = None
    rch_id: Optional[str] = None
    hpr_id: Optional[str] = None
    ward_id: Optional[int] = None
    sub_centre_id: Optional[int] = None
    pregnancy_outcome: Optional[str] = None
    outcome_date: Optional[date] = None
    is_active: Optional[bool] = None

class PregnantWomanResponse(PregnantWomanBase):
    id: int
    # aadhaar_masked: Optional[str] = None  # XXXXXXXX9012 — shown to frontend instead of hash
    pregnancy_registration_date: date
    is_self_registered: bool
    registration_approved: bool
    is_active: bool
    created_at: datetime

class PregnantWomanBulkUpload(BaseSchema):
    records: List[PregnantWomanCreate]

# ANC Visit Schemas
class ANCVisitBase(BaseSchema):
    pregnant_woman_id: int
    visit_number: int
    visit_date: date
    weight: Optional[float] = None
    blood_pressure: Optional[str] = None
    hemoglobin: Optional[float] = None
    urine_albumin: Optional[str] = None
    urine_sugar: Optional[str] = None
    fundal_height: Optional[float] = None
    fetal_heart_rate: Optional[int] = None
    referred_for_usg: bool = False
    is_emergency: bool = False
    doctor_notes: Optional[str] = None
    next_visit_date: Optional[date] = None
    facility_name: Optional[str] = None

class ANCVisitCreate(ANCVisitBase):
    pass

class ANCVisitUpdate(BaseSchema):
    visit_date: Optional[date] = None
    weight: Optional[float] = None
    blood_pressure: Optional[str] = None
    hemoglobin: Optional[float] = None
    urine_albumin: Optional[str] = None
    urine_sugar: Optional[str] = None
    fundal_height: Optional[float] = None
    fetal_heart_rate: Optional[int] = None
    referred_for_usg: Optional[bool] = None
    is_emergency: Optional[bool] = None
    doctor_notes: Optional[str] = None
    next_visit_date: Optional[date] = None
    facility_name: Optional[str] = None

class ANCVisitResponse(ANCVisitBase):
    id: int
    attended_by: Optional[int]
    created_at: datetime
    updated_at: datetime
    high_risk_triggered: bool = False
    risk_reasons: List[str] = []

# USG Appointment Schemas
class USGAppointmentBase(BaseSchema):
    pregnant_woman_id: int
    usg_centre_id: int
    scheduled_date: datetime
    appointment_type: AppointmentTypeEnum = AppointmentTypeEnum.REGULAR

class USGAppointmentCreate(USGAppointmentBase):
    pass

class USGAppointmentUpdate(BaseSchema):
    scheduled_date: Optional[datetime] = None
    status: Optional[AppointmentStatusEnum] = None
    reschedule_reason: Optional[str] = None

class USGAppointmentComplete(BaseSchema):
    completed_date: datetime
    scan_date: date
    gestational_age: Optional[str] = None
    trimester: str  # First, Second, Third
    scan_type: str  # Dating, Anomaly, Growth, Doppler
    findings: str  # Normal, Abnormal
    abnormal_findings: Optional[List[str]] = None  # List of abnormalities if findings=Abnormal
    additional_notes: Optional[str] = None
    doctor_name: str
    technician_name: str
    usg_findings: Optional[str] = None
    is_high_risk: bool = False
    confirmed_edd_date: date  # required — USG doctor-confirmed EDD

class USGAppointmentResponse(USGAppointmentBase):
    id: int
    pregnant_woman_name: Optional[str] = None
    status: AppointmentStatusEnum
    reschedule_count: int
    completed_date: Optional[datetime]
    sms_sent: bool
    created_at: datetime
    prescription_file_url: Optional[str] = None
    report_file_url: Optional[str] = None

# Feedback Schemas
class FeedbackRecordBase(BaseSchema):
    pregnant_woman_id: int
    usg_appointment_id: int
    call_attempt: int
    call_datetime: datetime
    call_status: str
    response: str
    additional_comments: Optional[str] = None

class FeedbackRecordCreate(FeedbackRecordBase):
    pass

class FeedbackRecordResponse(FeedbackRecordBase):
    id: int
    feedback_completed: bool
    created_at: datetime

# Grievance Schemas
class GrievanceBase(BaseSchema):
    name: str
    mobile_number: str
    ward_id: Optional[int] = None
    block_id: int
    district_id: Optional[int] = None
    grievance_note: str
    rch_id: Optional[str] = None

class GrievanceCreate(GrievanceBase):
    pregnant_woman_id: Optional[int] = None

class GrievanceUpdate(BaseSchema):
    status: Optional[GrievanceStatusEnum] = None
    resolution_note: Optional[str] = None

class GrievanceResponse(BaseSchema):
    id: int
    ticket_number: str
    name: str
    mobile_number: str
    ward_id: Optional[int] = None
    block_id: int
    district_id: Optional[int] = None
    grievance_note: str
    rch_id: Optional[str] = None
    status: GrievanceStatusEnum
    escalated_to_district: bool
    created_at: datetime
    resolved_at: Optional[datetime]
    resolution_note: Optional[str] = None
    attachment_file_url: Optional[str] = None

# Notification Schemas
class NotificationBase(BaseSchema):
    title: str
    message: str
    notification_type: str
    reference_id: Optional[int] = None
    reference_type: Optional[str] = None

class NotificationCreate(NotificationBase):
    user_id: int

class NotificationResponse(NotificationBase):
    id: int
    is_read: bool
    created_at: datetime

# Notification Schemas
class NotificationBase(BaseSchema):
    title: str
    message: str
    notification_type: str
    category: str = "system"
    priority: str = "normal"
    reference_id: Optional[int] = None
    reference_type: Optional[str] = None
    action_url: Optional[str] = None
    extra_data: Optional[dict] = None

class NotificationCreate(NotificationBase):
    user_id: int

class NotificationResponse(NotificationBase):
    id: int
    user_id: int
    is_read: bool
    read_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    created_at: datetime

class NotificationUpdate(BaseSchema):
    is_read: Optional[bool] = None

class NotificationStats(BaseSchema):
    total: int
    unread: int
    by_category: dict
    by_priority: dict

# Bulk Upload Schemas
class BulkUploadResponse(BaseSchema):
    id: int
    file_name: str
    total_records: int
    successful_records: int
    failed_records: int
    duplicate_records: int
    processing_status: str
    created_at: datetime

# Dashboard Schemas
class DashboardStats(BaseSchema):
    total_pregnant_women: int
    active_pregnant_women: int
    total_usg_appointments: int
    pending_usg_appointments: int
    completed_usg_appointments: int
    rescheduled_usg_appointments: int = 0
    # USG dashboard KPIs: active = scheduled + accepted + rescheduled (not completed / cancelled)
    active_usg_appointments: int = 0
    cancelled_usg_appointments: int = 0
    usg_completion_rate: float = 0.0
    high_risk_cases: int
    active_high_risk_cases: int = 0
    pending_grievances: int
    resolved_grievances: int
    # Delivery stats
    total_referrals: int = 0
    pending_referrals: int = 0
    accepted_referrals: int = 0
    completed_deliveries: int = 0
    total_outcomes: int = 0
    # Maternal & infant mortality (§1.1 District dashboard KPI)
    maternal_deaths: int = 0
    infant_deaths: int = 0
    still_births: int = 0
    # PMSMA session stats (§1.4 PMSMA dashboard KPI)
    total_pmsma_sessions: int = 0
    pending_pmsma_sessions: int = 0
    active_pmsma_sessions: int = 0
    completed_pmsma_sessions: int = 0

class BlockDashboardStats(DashboardStats):
    block_id: int
    block_name: str
    total_sub_centres: int

class DistrictDashboardStats(BaseSchema):
    total_blocks: int
    total_sub_centres: int
    total_usg_centres: int
    overall_stats: DashboardStats
    block_wise_stats: List[BlockDashboardStats]

# SMS Schemas
class SMSRequest(BaseSchema):
    mobile_number: str
    message: str
    message_type: str
    pregnant_woman_id: Optional[int] = None
    usg_appointment_id: Optional[int] = None

class SMSResponse(BaseSchema):
    id: int
    mobile_number: str
    sent_at: datetime
    delivery_status: str

# Report Schemas
class ReportFilter(BaseSchema):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    district_id: Optional[int] = None
    block_id: Optional[int] = None
    sub_centre_id: Optional[int] = None
    is_high_risk: Optional[bool] = None

class AppointmentReport(BaseSchema):
    total_appointments: int
    scheduled: int
    completed: int
    cancelled: int
    emergency_cases: int
    high_risk_cases: int

class GrievanceReport(BaseSchema):
    total_grievances: int
    pending: int
    resolved: int
    escalated: int
    avg_resolution_time_days: float

# Response wrapper
class ResponseWrapper(BaseSchema):
    success: bool
    message: str
    data: Optional[dict] = None

class PaginatedResponse(BaseSchema):
    items: List[dict]
    total: int
    page: int
    page_size: int
    total_pages: int

# Report Schemas
class ReportPeriod(BaseSchema):
    start_date: date
    end_date: date

class DistrictSummary(BaseSchema):
    total_pregnant_women: int
    high_risk_cases: int
    self_registered: int
    high_risk_percentage: float

class BlockWiseData(BaseSchema):
    block_name: str
    total_women: int
    high_risk: int
    self_registered: int

class USGStatistics(BaseSchema):
    total_appointments: int
    completed_appointments: int
    emergency_appointments: int
    completion_rate: float

class GrievanceStatistics(BaseSchema):
    total_grievances: int
    resolved_grievances: int
    escalated_grievances: int
    resolution_rate: float

class DistrictPerformanceReport(BaseSchema):
    report_period: ReportPeriod
    district_summary: DistrictSummary
    block_wise_data: List[BlockWiseData]
    usg_statistics: USGStatistics
    grievance_statistics: GrievanceStatistics

class WardWiseData(BaseSchema):
    ward_name: str
    total_pregnant_women: int
    high_risk_cases: int
    approved_registrations: int
    self_registrations: int
    total_anc_visits: int
    usg_referrals: int
    emergency_visits: int

class BlockSummary(BaseSchema):
    total_wards: int
    total_pregnant_women: int
    total_high_risk: int
    total_anc_visits: int

class BlockWardWiseReport(BaseSchema):
    report_period: ReportPeriod
    block_summary: BlockSummary
    ward_wise_data: List[WardWiseData]

class SubCentreActivitySummary(BaseSchema):
    pregnant_women_under_care: int
    anc_visits_conducted: int
    usg_referrals_made: int
    average_visits_per_day: float

class SubCentreActivityReport(BaseSchema):
    report_period: ReportPeriod
    activity_summary: SubCentreActivitySummary

class USGAppointmentSummary(BaseSchema):
    total_appointments: int
    completed_appointments: int
    emergency_appointments: int
    completion_rate: float
    average_appointments_per_day: float

class USGCentreAppointmentReport(BaseSchema):
    report_period: ReportPeriod
    appointment_summary: USGAppointmentSummary

# Delivery Point Schemas
class DeliveryPointBase(BaseSchema):
    name: str
    code: str
    address: Optional[str] = None
    contact_number: Optional[str] = None
    contact_person_name: Optional[str] = None
    district_id: int
    block_id: Optional[int] = None
    is_sdh_dhh: bool = False

class DeliveryPointCreate(DeliveryPointBase):
    pass

class DeliveryPointUpdate(BaseSchema):
    name: Optional[str] = None
    code: Optional[str] = None
    address: Optional[str] = None
    contact_number: Optional[str] = None
    contact_person_name: Optional[str] = None
    block_id: Optional[int] = None
    is_sdh_dhh: Optional[bool] = None
    is_active: Optional[bool] = None

class DeliveryPointResponse(DeliveryPointBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

# PMSMA Centre Schemas
class PMSMACentreBase(BaseSchema):
    name: str
    code: str
    address: Optional[str] = None
    contact_number: Optional[str] = None
    contact_person_name: Optional[str] = None
    district_id: int
    block_id: Optional[int] = None

class PMSMACentreCreate(PMSMACentreBase):
    pass

class PMSMACentreUpdate(BaseSchema):
    name: Optional[str] = None
    code: Optional[str] = None
    address: Optional[str] = None
    contact_number: Optional[str] = None
    contact_person_name: Optional[str] = None
    block_id: Optional[int] = None
    is_active: Optional[bool] = None

class PMSMACentreResponse(PMSMACentreBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

# Delivery Referral Schemas
class DeliveryReferralCreate(BaseSchema):
    pregnant_woman_id: int
    dp_id: int
    observation_notes: Optional[str] = None

class DeliveryReferralReRefer(BaseSchema):
    new_dp_id: int
    re_refer_reason: str

class DeliveryReferralResponse(BaseSchema):
    id: int
    pregnant_woman_id: int
    referred_by_user_id: int
    sub_centre_id: Optional[int]
    dp_id: int
    previous_referral_id: Optional[int]
    observation_notes: Optional[str]
    status: ReferralStatusEnum
    re_refer_reason: Optional[str]
    accepted_by_user_id: Optional[int]
    accepted_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

# Admission Schemas
class AdmissionCreate(BaseSchema):
    admission_date: datetime
    treating_doctor: Optional[str] = None
    condition_at_admission: Optional[str] = None


class AdmissionUpdate(BaseSchema):
    treating_doctor: Optional[str] = None
    condition_at_admission: Optional[str] = None
    discharge_date: Optional[datetime] = None


class AdmissionResponse(BaseSchema):
    id: int
    referral_id: int
    pregnant_woman_id: int
    dp_id: int
    admission_date: datetime
    treating_doctor: Optional[str] = None
    condition_at_admission: Optional[str] = None
    discharge_date: Optional[datetime] = None
    days_admitted: int
    admitted_by: int
    created_at: datetime


# Delivery Outcome Schemas
BABY_STATUSES = {"live_birth", "still_birth", "infant_death"}
DELIVERY_TYPES_REQUIRING_BABIES = {
    DeliveryTypeEnum.SAFE_DELIVERY,
    DeliveryTypeEnum.LIVE_BIRTH,
    DeliveryTypeEnum.STILL_BIRTH,
    DeliveryTypeEnum.INFANT_DEATH,
}


class BabyCreate(BaseSchema):
    gender: BabyGenderEnum
    status: str  # live_birth | still_birth | infant_death

    @validator("status")
    def validate_status(cls, v):
        if v not in BABY_STATUSES:
            raise ValueError(f"status must be one of {sorted(BABY_STATUSES)}")
        return v


class BabyResponse(BaseSchema):
    baby_number: int
    gender: str
    status: str


class DeliveryOutcomeCreate(BaseSchema):
    delivery_type: DeliveryTypeEnum
    delivery_date: datetime
    babies: Optional[List[BabyCreate]] = None
    maternal_outcome: MaternalOutcomeEnum = MaternalOutcomeEnum.HEALTHY
    maternal_outcome_notes: Optional[str] = None
    remarks: Optional[str] = None

    @validator("babies", always=True)
    def validate_babies(cls, v, values):
        delivery_type = values.get("delivery_type")
        if delivery_type in DELIVERY_TYPES_REQUIRING_BABIES:
            if not v or len(v) == 0:
                raise ValueError("At least 1 baby is required for this delivery type")
            if len(v) > 5:
                raise ValueError("Maximum 5 babies allowed")
        if delivery_type == DeliveryTypeEnum.MATERNAL_DEATH:
            if v and len(v) > 0:
                raise ValueError("babies must not be provided for maternal_death")
        return v

    @validator("maternal_outcome_notes", always=True)
    def validate_maternal_outcome_notes(cls, v, values):
        maternal_outcome = values.get("maternal_outcome")
        if maternal_outcome in (MaternalOutcomeEnum.COMPLICATIONS, MaternalOutcomeEnum.MATERNAL_DEATH, MaternalOutcomeEnum.REFERRED_HIGHER_FACILITY):
            if not v or not v.strip():
                raise ValueError("maternal_outcome_notes is required when maternal_outcome is not 'healthy'")
        return v


class DeliveryOutcomeResponse(BaseSchema):
    id: int
    referral_id: int
    pregnant_woman_id: int
    dp_id: int
    delivery_type: DeliveryTypeEnum
    delivery_date: datetime
    baby_gender: Optional[BabyGenderEnum] = None  # deprecated — kept for backward compat
    baby_count: int = 0
    babies: List[BabyResponse] = []
    maternal_outcome: MaternalOutcomeEnum
    maternal_outcome_notes: Optional[str] = None
    remarks: Optional[str]
    recorded_by: int
    created_at: datetime


# Discharge Schemas
class DischargeCreate(BaseSchema):
    discharge_date: datetime
    discharge_facility: Optional[str] = None
    discharging_doctor: Optional[str] = None
    condition_at_discharge: Optional[str] = None
    discharge_notes: Optional[str] = None


class DischargeResponse(BaseSchema):
    id: int
    referral_id: int
    outcome_id: int
    pregnant_woman_id: int
    dp_id: int
    discharge_date: datetime
    discharge_facility: Optional[str] = None
    discharging_doctor: Optional[str] = None
    condition_at_discharge: Optional[str] = None
    discharge_notes: Optional[str] = None
    discharged_by: int
    created_at: datetime


# ECG Report Schemas
class ECGResultEnum(str, Enum):
    NORMAL = "normal"
    ABNORMAL = "abnormal"

class ECGReportCreate(BaseSchema):
    pregnant_woman_id: int
    ecg_date: date
    result: ECGResultEnum
    notes: Optional[str] = None

class ECGReportResponse(BaseSchema):
    id: int
    pregnant_woman_id: int
    dp_id: int
    recorded_by_user_id: int
    ecg_date: date
    result: ECGResultEnum
    notes: Optional[str] = None
    report_file_url: Optional[str] = None
    pregnant_woman_name: Optional[str] = None
    dp_name: Optional[str] = None
    recorded_by_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# Registration Status Check Schemas (public endpoint)
class RegistrationStatusSubCentre(BaseSchema):
    name: str
    address: Optional[str] = None
    contact_number: Optional[str] = None

class RegistrationStatusANM(BaseSchema):
    full_name: str
    mobile_number: str

class RegistrationStatusData(BaseSchema):
    full_name: str
    registration_date: date
    rch_id: Optional[str] = None
    sub_centre: Optional[RegistrationStatusSubCentre] = None
    anm: Optional[RegistrationStatusANM] = None

class RegistrationStatusResponse(BaseSchema):
    status: str  # approved | pending | rejected | not_found | invalid
    message: str
    data: Optional[RegistrationStatusData] = None