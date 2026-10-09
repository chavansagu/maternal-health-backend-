from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, ForeignKey, Enum, Date, Float, UniqueConstraint, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

Base = declarative_base()

# Enums
class UserRole(str, enum.Enum):
    DISTRICT = "district"
    BLOCK = "block"
    SUB_CENTRE = "sub_centre"
    USG_CENTRE = "usg_centre"
    DP = "dp"
    PMSMA = "pmsma"

class GrievanceStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    ESCALATED = "escalated"

class AppointmentStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    ACCEPTED = "accepted"
    RESCHEDULED = "rescheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class AppointmentType(str, enum.Enum):
    REGULAR = "regular"
    EMERGENCY = "emergency"

class ReferralStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    RE_REFERRED = "re_referred"
    COMPLETED = "completed"

class DeliveryType(str, enum.Enum):
    SAFE_DELIVERY = "safe_delivery"
    LIVE_BIRTH = "live_birth"
    STILL_BIRTH = "still_birth"
    INFANT_DEATH = "infant_death"
    MATERNAL_DEATH = "maternal_death"

class BabyGender(str, enum.Enum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    
class FeedbackResponse(str, enum.Enum):
    FREE = "1"
    NOT_FREE = "2"
    NO_RESPONSE = "no_response"

# User Management
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(Enum(UserRole, values_callable=lambda x: [e.value for e in x]), nullable=False)
    full_name = Column(String(255), nullable=False)
    mobile_number = Column(String(15), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    # Foreign Keys
    district_id = Column(Integer, ForeignKey("districts.id"), nullable=True)
    block_id = Column(Integer, ForeignKey("blocks.id"), nullable=True)
    sub_centre_id = Column(Integer, ForeignKey("sub_centres.id"), nullable=True)
    usg_centre_id = Column(Integer, ForeignKey("usg_centres.id"), nullable=True)
    dp_id = Column(Integer, ForeignKey("delivery_points.id"), nullable=True)
    pmsma_centre_id = Column(Integer, ForeignKey("pmsma_centres.id"), nullable=True)
    
    # Relationships
    district = relationship("District", back_populates="users")
    block = relationship("Block", back_populates="users")
    sub_centre = relationship("SubCentre", back_populates="users")
    usg_centre = relationship("USGCentre", back_populates="users")
    delivery_point = relationship("DeliveryPoint", back_populates="users")
    pmsma_centre = relationship("PMSMACentre", back_populates="users")

# Administrative Divisions
class District(Base):
    __tablename__ = "districts"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    code = Column(String(50), unique=True, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # Relationships
    users = relationship("User", back_populates="district")
    blocks = relationship("Block", back_populates="district")
    grievances = relationship("Grievance", back_populates="district")

class Block(Base):
    __tablename__ = "blocks"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    name_regional = Column(String(255), nullable=True)
    code = Column(String(50), unique=True, nullable=False)
    district_id = Column(Integer, ForeignKey("districts.id"), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # Relationships
    district = relationship("District", back_populates="blocks")
    users = relationship("User", back_populates="block")
    sub_centres = relationship("SubCentre", back_populates="block")
    wards = relationship("Ward", back_populates="block")
    grievances = relationship("Grievance", back_populates="block", foreign_keys="Grievance.assigned_to_block")

class Ward(Base):
    __tablename__ = "wards"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    name_regional = Column(String(255), nullable=True)
    code = Column(String(50), nullable=False)
    block_id = Column(Integer, ForeignKey("blocks.id"), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    
    # Relationships
    block = relationship("Block", back_populates="wards")
    sub_centres = relationship("SubCentre", secondary="ward_subcentre_mapping", overlaps="wards")
    pregnant_women = relationship("PregnantWoman", back_populates="ward")

class SubCentre(Base):
    __tablename__ = "sub_centres"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    code = Column(String(50), unique=True, nullable=False)
    block_id = Column(Integer, ForeignKey("blocks.id"), nullable=False)
    address = Column(Text)
    contact_number = Column(String(15))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # Relationships
    block = relationship("Block", back_populates="sub_centres")
    users = relationship("User", back_populates="sub_centre")
    wards = relationship("Ward", secondary="ward_subcentre_mapping", overlaps="sub_centres")
    pregnant_women = relationship("PregnantWoman", back_populates="sub_centre")

class WardSubcentreMapping(Base):
    __tablename__ = "ward_subcentre_mapping"
    
    id = Column(Integer, primary_key=True, index=True)
    ward_id = Column(Integer, ForeignKey("wards.id"), nullable=False)
    sub_centre_id = Column(Integer, ForeignKey("sub_centres.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.now)

class UserWardMapping(Base):
    __tablename__ = "user_ward_mapping"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    ward_id = Column(Integer, ForeignKey("wards.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.now)

class BlockSubcentreMapping(Base):
    __tablename__ = "block_subcentre_mapping"
    
    id = Column(Integer, primary_key=True, index=True)
    block_id = Column(Integer, ForeignKey("blocks.id"), nullable=False)
    sub_centre_id = Column(Integer, ForeignKey("sub_centres.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.now)

class USGCentreBlockMapping(Base):
    __tablename__ = "usg_centre_block_mapping"
    
    id = Column(Integer, primary_key=True, index=True)
    usg_centre_id = Column(Integer, ForeignKey("usg_centres.id"), nullable=False)
    block_id = Column(Integer, ForeignKey("blocks.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.now)

class USGCentre(Base):
    __tablename__ = "usg_centres"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    code = Column(String(50), unique=True, nullable=False)
    address = Column(Text)
    contact_number = Column(String(15))
    contact_person_name = Column(String(255))  # NEW FIELD
    email = Column(String(255))
    google_map_url = Column(String(500))  # NEW FIELD
    is_empanelled = Column(Boolean, default=True)
    is_private = Column(Boolean, default=False)
    district_id = Column(Integer, ForeignKey("districts.id"))
    block_id = Column(Integer, ForeignKey("blocks.id"), nullable=True)
    ward_id = Column(Integer, ForeignKey("wards.id"), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # Relationships
    users = relationship("User", back_populates="usg_centre")
    appointments = relationship("USGAppointment", back_populates="usg_centre")

# Pregnant Women and Medical Records
class PregnantWoman(Base):
    __tablename__ = "pregnant_women"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Unique Identifiers
    abha_id = Column(String(50), unique=True, nullable=True, index=True)
    rch_id = Column(String(50), unique=True, nullable=True, index=True)
    # aadhaar_number = Column(String(255), unique=True, nullable=True, index=True)  # bcrypt hash
    # aadhaar_masked = Column(String(20), nullable=True)  # XXXXXXXX9012 for display
    mobile_number = Column(String(15), nullable=False, index=True)
    
    # Personal Information
    full_name = Column(String(255), nullable=False)
    husband_name = Column(String(255))
    date_of_birth = Column(Date)
    age = Column(Integer)
    address = Column(Text)
    
    # Administrative Links
    ward_id = Column(Integer, ForeignKey("wards.id"))
    block_id = Column(Integer, ForeignKey("blocks.id"))
    district_id = Column(Integer, ForeignKey("districts.id"))
    sub_centre_id = Column(Integer, ForeignKey("sub_centres.id"))
    
    # Pregnancy Information
    lmp_date = Column(Date)  # Last Menstrual Period
    edd_date = Column(Date)  # Expected Date of Delivery
    pregnancy_registration_date = Column(Date, default=datetime.now)
    gravida = Column(Integer)  # Number of pregnancies
    para = Column(Integer)  # Number of births
    
    # Medical Information
    hpr_id = Column(String(50))  # Healthcare Professional Registry ID
    blood_group = Column(String(10))
    is_high_risk = Column(Boolean, default=False)
    risk_factors = Column(Text)
    
    # Registration Source
    is_self_registered = Column(Boolean, default=False)
    registered_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    registration_approved = Column(Boolean, default=False)
    
    # Status
    is_active = Column(Boolean, default=True)
    pregnancy_outcome = Column(String(50))  # delivered, miscarriage, etc.
    outcome_date = Column(Date)
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # Relationships
    ward = relationship("Ward", back_populates="pregnant_women")
    sub_centre = relationship("SubCentre", back_populates="pregnant_women")
    anc_visits = relationship("ANCVisit", back_populates="pregnant_woman")
    usg_appointments = relationship("USGAppointment", back_populates="pregnant_woman")
    grievances = relationship("Grievance", back_populates="pregnant_woman")
    feedback_records = relationship("FeedbackRecord", back_populates="pregnant_woman")
    delivery_referrals = relationship("DeliveryReferral", back_populates="pregnant_woman")
    ecg_reports = relationship("ECGReport", back_populates="pregnant_woman")

# ANC Visits
class ANCVisit(Base):
    __tablename__ = "anc_visits"
    
    id = Column(Integer, primary_key=True, index=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False)
    visit_number = Column(Integer, nullable=False)  # 1st, 2nd, 3rd, 4th ANC
    visit_date = Column(Date, nullable=False)
    
    # Clinical Details
    weight = Column(Float)
    blood_pressure = Column(String(20))
    hemoglobin = Column(Float)
     # NEW - Urine Test
    urine_albumin = Column(String(20), nullable=True)
    urine_sugar = Column(String(20), nullable=True)
    fundal_height = Column(Float)
    fetal_heart_rate = Column(Integer)
    
    # Prescriptions and Referrals
    referred_for_usg = Column(Boolean, default=False)
    is_emergency = Column(Boolean, default=False)
    doctor_notes = Column(Text)
    next_visit_date = Column(Date)
    
    # Healthcare Provider
    attended_by = Column(Integer, ForeignKey("users.id"))
    facility_name = Column(String(255))
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # Relationships
    pregnant_woman = relationship("PregnantWoman", back_populates="anc_visits")

# USG Appointments
class USGAppointment(Base):
    __tablename__ = "usg_appointments"
    
    id = Column(Integer, primary_key=True, index=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False)
    usg_centre_id = Column(Integer, ForeignKey("usg_centres.id"), nullable=False)
    
    # Scheduling Details
    scheduled_date = Column(DateTime, nullable=False)
    appointment_type = Column(Enum(AppointmentType), default=AppointmentType.REGULAR)
    status = Column(Enum(AppointmentStatus), default=AppointmentStatus.SCHEDULED)
    prescription_file_path = Column(String(500))  # Doctor's prescription (first file, backward compat)
    prescription_file_paths = Column(Text, nullable=True)  # JSON array of all prescription file paths
    
    # Rescheduling
    original_scheduled_date = Column(DateTime)
    reschedule_count = Column(Integer, default=0)
    reschedule_reason = Column(Text)
    
    # Completion Details
    completed_date = Column(DateTime)
    scan_date = Column(Date)
    gestational_age = Column(String(50))
    trimester = Column(String(20))  # First, Second, Third
    scan_type = Column(String(50))  # Dating, Anomaly, Growth, Doppler
    # findings = Column(String(50))  # Normal, Abnormal
    findings = Column(Text)  # Normal, Abnormal or detailed JSON for multiple findings
    abnormal_findings = Column(Text)  # JSON array of selected abnormalities
    additional_notes = Column(Text)
    doctor_name = Column(String(255))
    technician_name = Column(String(255))
    usg_findings = Column(Text)  # Detailed findings
    report_file_path = Column(String(500))  # First report file (backward compat)
    report_file_paths = Column(Text, nullable=True)  # JSON array of all report file paths
    is_high_risk = Column(Boolean, default=False)
    
    # Scheduling Info
    scheduled_by = Column(Integer, ForeignKey("users.id"))
    accepted_by = Column(Integer, ForeignKey("users.id"))
    completed_by = Column(Integer, ForeignKey("users.id"))
    
    # Notifications
    sms_sent = Column(Boolean, default=False)
    sms_sent_at = Column(DateTime)
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # Relationships
    pregnant_woman = relationship("PregnantWoman", back_populates="usg_appointments")
    usg_centre = relationship("USGCentre", back_populates="appointments")

# Feedback System
class FeedbackRecord(Base):
    __tablename__ = "feedback_records"
    
    id = Column(Integer, primary_key=True, index=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False)
    usg_appointment_id = Column(Integer, ForeignKey("usg_appointments.id"), nullable=False)
    
    # Call Details
    call_attempt = Column(Integer, default=1)  # 1 or 2
    call_datetime = Column(DateTime)
    call_status = Column(String(50))  # connected, not_reachable, busy, etc.
    
    # Feedback Response
    response = Column(Enum(FeedbackResponse), default=FeedbackResponse.NO_RESPONSE)
    additional_comments = Column(Text)
    
    # Next Call Schedule
    next_call_scheduled = Column(DateTime)
    feedback_completed = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # Relationships
    pregnant_woman = relationship("PregnantWoman", back_populates="feedback_records")

# Grievance Management
class Grievance(Base):
    __tablename__ = "grievances"
    
    id = Column(Integer, primary_key=True, index=True)
    ticket_number = Column(String(50), unique=True, nullable=False)
    
    # Grievance Details
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=True)
    name = Column(String(255), nullable=False)
    mobile_number = Column(String(15), nullable=False)
    rch_id = Column(String(50), nullable=True)
    ward_id = Column(Integer, ForeignKey("wards.id"))
    block_id = Column(Integer, ForeignKey("blocks.id"))
    district_id = Column(Integer, ForeignKey("districts.id"))
    
    # Description
    grievance_note = Column(Text, nullable=False)
    attachment_path = Column(String(500))
    
    # Status Management
    status = Column(Enum(GrievanceStatus), default=GrievanceStatus.PENDING)
    assigned_to_block = Column(Integer, ForeignKey("blocks.id"))
    assigned_to_user = Column(Integer, ForeignKey("users.id"))
    
    # Resolution
    resolution_note = Column(Text)
    resolved_by = Column(Integer, ForeignKey("users.id"))
    resolved_at = Column(DateTime)
    
    # Escalation
    escalated_to_district = Column(Boolean, default=False)
    escalation_date = Column(DateTime)
    auto_escalated = Column(Boolean, default=False)  # Auto-escalated after 7 days
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # Relationships
    pregnant_woman = relationship("PregnantWoman", back_populates="grievances")
    block = relationship("Block", back_populates="grievances", foreign_keys=[assigned_to_block])
    district = relationship("District", back_populates="grievances")

# SMS Logs
class SMSLog(Base):
    __tablename__ = "sms_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    mobile_number = Column(String(15), nullable=False)
    message = Column(Text, nullable=False)
    message_type = Column(String(50))  # appointment, health_tip, reminder, etc.
    
    # Reference
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=True)
    usg_appointment_id = Column(Integer, ForeignKey("usg_appointments.id"), nullable=True)
    
    # Delivery Status
    sent_at = Column(DateTime)
    delivery_status = Column(String(50))  # sent, delivered, failed
    provider_response = Column(Text)
    
    created_at = Column(DateTime, default=datetime.now)

# Bulk Upload Tracking
class BulkUpload(Base):
    __tablename__ = "bulk_uploads"
    
    id = Column(Integer, primary_key=True, index=True)
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500))
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    # Processing Stats
    total_records = Column(Integer, default=0)
    successful_records = Column(Integer, default=0)
    failed_records = Column(Integer, default=0)
    duplicate_records = Column(Integer, default=0)
    
    # Status
    processing_status = Column(String(50), default="pending")  # pending, processing, completed, failed
    error_log = Column(Text)
    
    created_at = Column(DateTime, default=datetime.now)
    completed_at = Column(DateTime)

# System Notifications
class Notification(Base):
    __tablename__ = "notifications"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    
    # Notification Details
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    notification_type = Column(String(50), nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True, default="system")
    priority = Column(String(20), nullable=False, default="normal")
    
    # Reference
    reference_id = Column(Integer, index=True)
    reference_type = Column(String(50))
    action_url = Column(String(500))
    
    # Metadata (JSON string)
    extra_data = Column(Text)  # Renamed from metadata to avoid SQLAlchemy conflict
    
    # Status
    is_read = Column(Boolean, default=False, index=True)
    read_at = Column(DateTime)
    deleted_at = Column(DateTime, index=True)
    
    created_at = Column(DateTime, default=datetime.now, index=True)
    
    # Relationships
    user = relationship("User")

# Password Reset Token
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token = Column(String(6), nullable=False)  # 6-digit OTP
    expires_at = Column(DateTime, nullable=False)
    is_used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    
    # Relationship
    user = relationship("User")

# Audit Log
class AuditLog(Base):
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    action = Column(String(100), nullable=False)
    entity_type = Column(String(100))
    entity_id = Column(Integer)
    old_values = Column(Text)
    new_values = Column(Text)
    ip_address = Column(String(50))
    user_agent = Column(String(255))
    created_at = Column(DateTime, default=datetime.now)

# Login Attempts
class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), nullable=False, index=True)
    ip_address = Column(String(50), nullable=False)
    success = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now, index=True)

# Delivery Point
class DeliveryPoint(Base):
    __tablename__ = "delivery_points"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    code = Column(String(50), unique=True, nullable=False)
    address = Column(Text)
    contact_number = Column(String(15))
    contact_person_name = Column(String(255))
    district_id = Column(Integer, ForeignKey("districts.id"), nullable=False)
    block_id = Column(Integer, ForeignKey("blocks.id"), nullable=True)
    is_sdh_dhh = Column(Boolean, default=False)  # True = SDH/DHH; shown to HRP cases only
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    users = relationship("User", back_populates="delivery_point")
    referrals = relationship("DeliveryReferral", back_populates="delivery_point", foreign_keys="DeliveryReferral.dp_id")


class DeliveryReferral(Base):
    __tablename__ = "delivery_referrals"

    id = Column(Integer, primary_key=True, index=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False)
    referred_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    sub_centre_id = Column(Integer, ForeignKey("sub_centres.id"), nullable=True)
    dp_id = Column(Integer, ForeignKey("delivery_points.id"), nullable=False)
    previous_referral_id = Column(Integer, ForeignKey("delivery_referrals.id"), nullable=True)  # chain
    observation_notes = Column(Text)
    status = Column(String(20), default="pending")
    re_refer_reason = Column(Text)
    treatment_given = Column(Text, nullable=True)  # Treatment given before re-refer
    re_refer_attachment_path = Column(String(500), nullable=True)  # First file (backward compat)
    re_refer_attachment_paths = Column(Text, nullable=True)  # JSON array of all file paths
    accepted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    accepted_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    pregnant_woman = relationship("PregnantWoman", back_populates="delivery_referrals")
    delivery_point = relationship("DeliveryPoint", back_populates="referrals", foreign_keys=[dp_id])
    referred_by = relationship("User", foreign_keys=[referred_by_user_id])
    accepted_by = relationship("User", foreign_keys=[accepted_by_user_id])
    sub_centre = relationship("SubCentre")
    previous_referral = relationship("DeliveryReferral", remote_side="DeliveryReferral.id", foreign_keys=[previous_referral_id])
    outcome = relationship("DeliveryOutcome", back_populates="referral", uselist=False)
    discharge = relationship("Discharge", back_populates="referral", uselist=False)
    admission = relationship("Admission", back_populates="referral", uselist=False)


# Admission — records the hospital stay between an accepted referral and the
# delivery outcome. Facility is implicitly the DP (dp_id), since a DP *is* the
# facility in this system (see DeliveryPoint). days_admitted is intentionally
# NOT stored — it's derived from admission_date/discharge_date, same pattern
# as PregnantWoman.edd_date being computed from lmp_date rather than a stale
# stored value that could drift.
class Admission(Base):
    __tablename__ = "admissions"

    id = Column(Integer, primary_key=True, index=True)
    referral_id = Column(Integer, ForeignKey("delivery_referrals.id"), nullable=False, unique=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False)
    dp_id = Column(Integer, ForeignKey("delivery_points.id"), nullable=False)

    admission_date = Column(DateTime, nullable=False)
    treating_doctor = Column(String(255), nullable=True)
    condition_at_admission = Column(String(100), nullable=True)
    discharge_date = Column(DateTime, nullable=True)

    admitted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    referral = relationship("DeliveryReferral", back_populates="admission")
    pregnant_woman = relationship("PregnantWoman")
    delivery_point = relationship("DeliveryPoint")
    admitted_by_user = relationship("User", foreign_keys=[admitted_by])

    @property
    def days_admitted(self) -> int:
        """Computed on read: (discharge_date or now()) - admission_date, in whole days."""
        if not self.admission_date:
            return 0
        end = self.discharge_date or datetime.now()
        delta = end - self.admission_date
        return max(delta.days, 0)


class MaternalOutcome(str, enum.Enum):
    HEALTHY = "healthy"
    COMPLICATIONS = "complications"
    REFERRED_HIGHER_FACILITY = "referred_higher_facility"
    MATERNAL_DEATH = "maternal_death"


class DeliveryOutcome(Base):
    __tablename__ = "delivery_outcomes"

    id = Column(Integer, primary_key=True, index=True)
    referral_id = Column(Integer, ForeignKey("delivery_referrals.id"), nullable=False, unique=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False)
    dp_id = Column(Integer, ForeignKey("delivery_points.id"), nullable=False)
    delivery_type = Column(Enum(DeliveryType), nullable=False)
    delivery_date = Column(DateTime, nullable=False)
    baby_gender = Column(String(10), nullable=True)  # deprecated — kept for backward compat
    baby_count = Column(Integer, nullable=False, default=0)

    # Maternal outcome — captured independently of delivery_type/baby outcomes.
    # (delivery_type still has a legacy 'maternal_death' value used by existing
    # reports — kept as-is for backward compatibility — but this field is now
    # the source of truth for the mother's condition after delivery, including
    # non-fatal cases like complications or referral onward.)
    maternal_outcome = Column(
        Enum(MaternalOutcome, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=MaternalOutcome.HEALTHY,
    )
    maternal_outcome_notes = Column(Text, nullable=True)

    remarks = Column(Text)
    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    referral = relationship("DeliveryReferral", back_populates="outcome")
    pregnant_woman = relationship("PregnantWoman")
    delivery_point = relationship("DeliveryPoint")
    recorded_by_user = relationship("User", foreign_keys=[recorded_by])
    babies = relationship("DeliveryOutcomeBaby", back_populates="outcome", cascade="all, delete-orphan")
    discharge = relationship("Discharge", back_populates="outcome", uselist=False)


class DeliveryOutcomeBaby(Base):
    __tablename__ = "delivery_outcome_babies"

    id = Column(Integer, primary_key=True, index=True)
    outcome_id = Column(Integer, ForeignKey("delivery_outcomes.id", ondelete="CASCADE"), nullable=False)
    baby_number = Column(Integer, nullable=False)
    gender = Column(String(10), nullable=False)
    status = Column(String(20), nullable=False)  # live_birth, still_birth, infant_death
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("outcome_id", "baby_number", name="uq_outcome_baby_number"),
        Index("ix_delivery_outcome_babies_outcome_id", "outcome_id"),
        Index("ix_delivery_outcome_babies_status", "status"),
    )

    outcome = relationship("DeliveryOutcome", back_populates="babies")


# Discharge — explicit closing step after a delivery outcome is recorded.
# Kept as its own table (rather than extending DeliveryOutcome) since it is a
# separate DP action that happens after the outcome, has its own actor/date,
# and is what triggers PNC reminder scheduling.
class Discharge(Base):
    __tablename__ = "discharges"

    id = Column(Integer, primary_key=True, index=True)
    referral_id = Column(Integer, ForeignKey("delivery_referrals.id"), nullable=False, unique=True)
    outcome_id = Column(Integer, ForeignKey("delivery_outcomes.id"), nullable=False, unique=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False)
    dp_id = Column(Integer, ForeignKey("delivery_points.id"), nullable=False)

    discharge_date = Column(DateTime, nullable=False)
    discharge_facility = Column(String(255), nullable=True)
    discharging_doctor = Column(String(255), nullable=True)
    condition_at_discharge = Column(String(50), nullable=True)  # e.g. stable, needs_followup, referred_higher_facility
    discharge_notes = Column(Text, nullable=True)

    discharged_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    referral = relationship("DeliveryReferral", back_populates="discharge")
    outcome = relationship("DeliveryOutcome", back_populates="discharge")
    pregnant_woman = relationship("PregnantWoman")
    delivery_point = relationship("DeliveryPoint")
    discharged_by_user = relationship("User", foreign_keys=[discharged_by])
    pnc_reminders = relationship("PNCReminder", back_populates="discharge", cascade="all, delete-orphan")


class PNCReminderStatus(str, enum.Enum):
    SCHEDULED = "scheduled"  # not yet due — waiting for its due_date
    DUE = "due"              # due_date has arrived — appears on the ANM/sub-centre worklist
    COMPLETED = "completed"  # ANM/sub-centre has recorded the PNC visit


class PNCReminder(Base):
    """
    Auto-created on Discharge — one row per standard PNC visit window
    (48hrs / day 7 / day 42 post-delivery by default, configurable).
    Surfaced as a trackable worklist item for the ANM/sub-centre, same
    pattern as MobilisationCase. No SMS/call/AI-call action here by design.
    """
    __tablename__ = "pnc_reminders"

    id = Column(Integer, primary_key=True, index=True)
    discharge_id = Column(Integer, ForeignKey("discharges.id", ondelete="CASCADE"), nullable=False, index=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False, index=True)
    sub_centre_id = Column(Integer, ForeignKey("sub_centres.id"), nullable=True, index=True)

    visit_label = Column(String(20), nullable=False)  # "48hr", "day7", "day42", ...
    due_date = Column(Date, nullable=False, index=True)

    status = Column(
        Enum(PNCReminderStatus, values_callable=lambda x: [e.value for e in x]),
        default=PNCReminderStatus.SCHEDULED,
        nullable=False,
        index=True,
    )

    completed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    remarks = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    discharge = relationship("Discharge", back_populates="pnc_reminders")
    pregnant_woman = relationship("PregnantWoman")
    sub_centre = relationship("SubCentre")
    completed_by_user = relationship("User", foreign_keys=[completed_by])

    __table_args__ = (
        Index("ix_pnc_reminder_status_scope", "status", "sub_centre_id"),
    )


# AI Reporting Models
class AIQueryHistory(Base):
    __tablename__ = "ai_query_history"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user_role = Column(String(50), nullable=False)
    
    # Query Details
    natural_query = Column(Text, nullable=False)
    generated_sql = Column(Text)
    
    # Execution Metrics
    execution_time_ms = Column(Integer)
    result_count = Column(Integer)
    success = Column(Boolean, default=False)
    error_message = Column(Text)
    
    # AI Provider Info
    ai_provider = Column(String(50))  # gemini, ollama, vanna, openai, hybrid
    
    # Store response data for history replay
    response_data = Column(Text)  # JSON string of data
    visualization_config = Column(Text)  # JSON string of visualization config

    # Async job status: processing | completed | failed
    job_status = Column(String(20), default="completed", nullable=False)

    created_at = Column(DateTime, default=datetime.now)
    
    # Relationships
    user = relationship("User")
    feedback = relationship("QueryFeedback", back_populates="query_history")

class SavedReport(Base):
    __tablename__ = "saved_reports"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    # Report Details
    report_name = Column(String(255), nullable=False)
    description = Column(Text)
    query_template = Column(Text, nullable=False)
    
    # Chart Configuration
    chart_type = Column(String(50))  # bar, line, pie, table, mixed
    chart_config = Column(Text)  # JSON string
    
    # Sharing
    is_public = Column(Boolean, default=False)
    
    # Usage Stats
    usage_count = Column(Integer, default=0)
    last_used_at = Column(DateTime)
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # Relationships
    user = relationship("User")

class QueryFeedback(Base):
    __tablename__ = "query_feedback"
    
    id = Column(Integer, primary_key=True, index=True)
    query_history_id = Column(Integer, ForeignKey("ai_query_history.id"), nullable=False)
    
    # Feedback
    was_helpful = Column(Boolean)
    feedback_text = Column(Text)
    rating = Column(Integer)  # 1-5 stars
    
    created_at = Column(DateTime, default=datetime.now)
    
    # Relationships
    query_history = relationship("AIQueryHistory", back_populates="feedback")


# ECG Report
class ECGResultEnum(str, enum.Enum):
    NORMAL = "normal"
    ABNORMAL = "abnormal"

class ECGReport(Base):
    __tablename__ = "ecg_reports"

    id = Column(Integer, primary_key=True, index=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False, index=True)
    dp_id = Column(Integer, ForeignKey("delivery_points.id"), nullable=False)
    recorded_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    ecg_date = Column(Date, nullable=False)
    result = Column(Enum(ECGResultEnum), nullable=False)
    notes = Column(Text, nullable=True)
    report_file_path = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    pregnant_woman = relationship("PregnantWoman", back_populates="ecg_reports")
    delivery_point = relationship("DeliveryPoint")
    recorded_by = relationship("User", foreign_keys=[recorded_by_user_id])


# EDD History
class EDDHistory(Base):
    __tablename__ = "edd_history"

    id = Column(Integer, primary_key=True, index=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False, index=True)
    previous_edd = Column(Date, nullable=True)
    new_edd = Column(Date, nullable=False)
    source = Column(String(50), nullable=False)  # "LMP" or "USG_CONFIRMED"
    changed_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    changed_at = Column(DateTime, default=datetime.now)

    pregnant_woman = relationship("PregnantWoman")


# PMSMA Session
class PMSMASession(Base):
    __tablename__ = "pmsma_sessions"

    id = Column(Integer, primary_key=True, index=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False, index=True)
    scheduled_date = Column(DateTime, nullable=False)
    original_scheduled_date = Column(DateTime, nullable=True)
    block_id = Column(Integer, ForeignKey("blocks.id"), nullable=True)
    site = Column(String(255), nullable=True)  # free-text site/camp name (legacy / fallback label)
    pmsma_centre_id = Column(Integer, ForeignKey("pmsma_centres.id"), nullable=True)  # structured site reference
    status = Column(String(20), default="scheduled", nullable=False)  # scheduled/completed/rescheduled/cancelled
    appointment_type = Column(String(20), default="regular", nullable=False)  # regular/emergency
    # Clinical fields (filled on completion)
    bp = Column(String(20), nullable=True)
    blood_sugar = Column(Float, nullable=True)
    hb = Column(Float, nullable=True)
    weight = Column(Float, nullable=True)
    additional_parameters = Column(Text, nullable=True)  # JSON
    counselling_notes = Column(Text, nullable=True)
    is_high_risk = Column(Boolean, default=False)
    # Scheduling metadata
    scheduled_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    completed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
        # Examination (spec F)
    visit_number = Column(Integer, default=1, nullable=False)
    visit_type = Column(String(20), default="regular", nullable=False)  # regular / additional
    visit_date = Column(Date, nullable=True)
    findings = Column(Text, nullable=True)

    # HRP conditions (yes/no)
    hrp_severe_anaemia = Column(Boolean, default=False, nullable=False)
    hrp_pih = Column(Boolean, default=False, nullable=False)
    hrp_gdm = Column(Boolean, default=False, nullable=False)
    hrp_hiv_reactive = Column(Boolean, default=False, nullable=False)
    hrp_syphilis = Column(Boolean, default=False, nullable=False)
    hrp_hypothyroidism = Column(Boolean, default=False, nullable=False)
    hrp_tuberculosis = Column(Boolean, default=False, nullable=False)
    hrp_malaria = Column(Boolean, default=False, nullable=False)
    hrp_previous_lscs = Column(Boolean, default=False, nullable=False)
    hrp_hepatitis_b = Column(Boolean, default=False, nullable=False)
    hrp_teenage_pregnancy = Column(Boolean, default=False, nullable=False)
    hrp_still_birth_history = Column(Boolean, default=False, nullable=False)
    hrp_rh_negative = Column(Boolean, default=False, nullable=False)
    hrp_early_primi = Column(Boolean, default=False, nullable=False)
    hrp_elderly_primi = Column(Boolean, default=False, nullable=False)
    hrp_multiple_pregnancy = Column(Boolean, default=False, nullable=False)

    # Recommendations
    recommended_action = Column(Text, nullable=True)
    recommended_dp_id = Column(Integer, ForeignKey("delivery_points.id"), nullable=True)
    usg_required = Column(Boolean, default=False, nullable=False)
    
    reschedule_reason = Column(Text, nullable=True)
    is_emergency_override = Column(Boolean, default=False)
    override_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    pregnant_woman = relationship("PregnantWoman")
    block = relationship("Block")
    pmsma_centre = relationship("PMSMACentre")
    scheduled_by_user = relationship("User", foreign_keys=[scheduled_by])
    completed_by_user = relationship("User", foreign_keys=[completed_by])


# PMSMA Centre
class PMSMACentre(Base):
    __tablename__ = "pmsma_centres"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    code = Column(String(50), unique=True, nullable=False)
    address = Column(Text)
    contact_number = Column(String(15))
    contact_person_name = Column(String(255))
    district_id = Column(Integer, ForeignKey("districts.id"), nullable=False)
    block_id = Column(Integer, ForeignKey("blocks.id"), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # Relationships
    users = relationship("User", back_populates="pmsma_centre")


# IVR Call Logs
class MobilisationStatus(str, enum.Enum):
    PENDING = "pending"
    MOBILISED = "mobilised"
    ESCALATED = "escalated"
    CLOSED = "closed"


class MobilisationCase(Base):
    """
    Auto-generated worklist of pregnant women who need a mobilisation nudge
    (missed ANC / missed PMSMA / missed USG / near-EDD / HRP not yet referred).
    Populated by scheduler jobs from existing signals — not hand-entered.
    """
    __tablename__ = "mobilisation_cases"

    id = Column(Integer, primary_key=True, index=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False, index=True)

    trigger_type = Column(String(50), nullable=False, index=True)  # missed_anc, missed_pmsma, missed_usg, near_edd, hrp_unreferred
    trigger_reference_id = Column(Integer, nullable=True)          # id of the ANCVisit/PMSMASession/USGAppointment row, if any
    trigger_detail = Column(Text, nullable=True)                   # human-readable summary shown on the card

    sub_centre_id = Column(Integer, ForeignKey("sub_centres.id"), nullable=True, index=True)
    block_id = Column(Integer, ForeignKey("blocks.id"), nullable=True, index=True)
    district_id = Column(Integer, ForeignKey("districts.id"), nullable=True, index=True)

    status = Column(
        Enum(MobilisationStatus, values_callable=lambda x: [e.value for e in x]),
        default=MobilisationStatus.PENDING,
        nullable=False,
        index=True,
    )
    escalation_level = Column(String(20), default="anm", nullable=False)  # anm -> block -> district
    escalated_at = Column(DateTime, nullable=True)

    mobilised_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    mobilised_at = Column(DateTime, nullable=True)
    remarks = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    pregnant_woman = relationship("PregnantWoman")
    sub_centre = relationship("SubCentre")
    block = relationship("Block")
    district = relationship("District")
    mobilised_by_user = relationship("User", foreign_keys=[mobilised_by])

    __table_args__ = (
        Index("ix_mobilisation_status_scope", "status", "sub_centre_id", "block_id", "district_id"),
    )


class IVRCallLog(Base):
    __tablename__ = "ivr_call_logs"

    id = Column(Integer, primary_key=True, index=True)
    pregnant_woman_id = Column(Integer, ForeignKey("pregnant_women.id"), nullable=False, index=True)
    mobile_number = Column(String(15), nullable=False)

    # Call type
    call_type = Column(String(50), nullable=False)  # high_risk_advisory, feedback_call_1, feedback_call_2

    # Status
    call_status = Column(String(50), default="initiated")  # initiated, connected, no_answer, busy, failed

    # Dovesoft response
    ivr_campaign_id = Column(String(100), nullable=True)  # Dovesoft campaign ID returned after trigger
    call_duration_seconds = Column(Integer, nullable=True)
    provider_response = Column(Text, nullable=True)  # raw JSON response from Dovesoft

    # Timestamps
    scheduled_at = Column(DateTime, nullable=True)
    called_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    # Relationship
    pregnant_woman = relationship("PregnantWoman")