from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, and_, or_, case, desc, distinct, false, select
from typing import List, Optional, Dict, Any
from datetime import datetime, date, timedelta
import pandas as pd
import io
import os
import hashlib
import json
import logging
from functools import lru_cache

from database import get_db
from models import (
    User, PregnantWoman, ANCVisit, USGAppointment, Grievance,
    District, Block, Ward, SubCentre, USGCentre, SMSLog,
    DeliveryReferral, DeliveryOutcome, DeliveryPoint, ECGReport,
    PMSMASession, PMSMACentre, MobilisationCase
)
from auth import get_current_active_user
from analytics_filters import (
    PWFilters, make_pw_filters_dep, pw_conditions, scope_conditions,
    gestational_weeks, trimester_of, trimester_condition, AGE_BANDS, AGE_BAND_LABELS,
    report_scope_conditions, sub_centre_match, has_any_filter, FULL_TERM_DAYS,
)
from models import Ward, WardSubcentreMapping, AppointmentStatus, AppointmentType

router = APIRouter(prefix="/reports", tags=["Reports"])
logger = logging.getLogger(__name__)

# Simple in-memory cache for reports
report_cache: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 60  # seconds - short on purpose: reports are read straight after data entry
CACHE_MAX_ENTRIES = 500

def get_cache_key(endpoint: str, params: dict) -> str:
    """Generate cache key from endpoint and parameters"""
    param_str = json.dumps(params, sort_keys=True, default=str)
    return hashlib.md5(f"{endpoint}:{param_str}".encode()).hexdigest()

def get_cached_report(cache_key: str) -> Optional[Dict[str, Any]]:
    """Get cached report if valid"""
    if cache_key in report_cache:
        cached = report_cache[cache_key]
        if datetime.now().timestamp() - cached['timestamp'] < CACHE_TTL:
            return cached['data']
        else:
            del report_cache[cache_key]
    return None

def set_cached_report(cache_key: str, data: Dict[str, Any]):
    """Cache report data"""
    if len(report_cache) >= CACHE_MAX_ENTRIES:
        now = datetime.now().timestamp()
        for k in [k for k, v in report_cache.items() if now - v['timestamp'] >= CACHE_TTL]:
            del report_cache[k]
        if len(report_cache) >= CACHE_MAX_ENTRIES:   # still full: drop the oldest half
            for k in sorted(report_cache, key=lambda k: report_cache[k]['timestamp'])[:CACHE_MAX_ENTRIES // 2]:
                del report_cache[k]
    report_cache[cache_key] = {
        'data': data,
        'timestamp': datetime.now().timestamp()
    }

def generate_visualization_config(data: list, chart_type: str, x_field: str, y_field: str, title: str) -> dict:
    """Generate chart configuration for frontend"""
    return {
        "type": chart_type,
        "title": title,
        "x_axis": x_field,
        "y_axis": y_field,
        "data": data
    }

# Column name mappings: db field -> friendly display name
COLUMN_LABELS = {
    # District Summary
    "total_pregnant_women": "Total Pregnant Women",
    "high_risk_cases": "High Risk Cases",
    "high_risk_percentage": "High Risk (%)",
    "self_registered": "Self Registered",
    # Block Wise Data
    "block_name": "Block Name",
    "total_women": "Total Women",
    "high_risk": "High Risk",
    "anc_visits": "ANC Visits",
    "usg_referrals": "USG Referrals",
    # USG Statistics
    "total_appointments": "Total Appointments",
    "completed_appointments": "Completed Appointments",
    "emergency_appointments": "Emergency Appointments",
    "completion_rate": "Completion Rate (%)",
    # Grievance Statistics
    "total_grievances": "Total Grievances",
    "resolved_grievances": "Resolved Grievances",
    "pending_grievances": "Pending Grievances",
    "escalated_grievances": "Escalated Grievances",
    "resolution_rate": "Resolution Rate (%)",
    "average_resolution_time_days": "Avg Resolution Time (Days)",
    # Ward Wise Data
    "ward_name": "Ward Name",
    "total_pregnant_women": "Total Pregnant Women",
    "high_risk_cases": "High Risk Cases",
    "approved_registrations": "Approved Registrations",
    "self_registrations": "Self Registrations",
    "total_anc_visits": "Total ANC Visits",
    "emergency_visits": "Emergency Visits",
    # Block Summary
    "total_wards": "Total Wards",
    "total_high_risk": "Total High Risk",
    # High Risk Tracking
    "id": "ID",
    "full_name": "Full Name",
    "mobile_number": "Mobile Number",
    "age": "Age",
    "risk_factors": "Risk Factors",
    "edd_date": "Expected Delivery Date",
    "last_anc_visit": "Last ANC Visit",
    "total_anc_visits": "Total ANC Visits",
    "usg_scans_completed": "USG Scans Completed",
    "block_name": "Block Name",
    "sub_centre_name": "Sub Centre Name",
    # ANC Compliance
    "expected_visits": "Expected Visits",
    "actual_visits": "Actual Visits",
    "compliance_status": "Compliance Status",
    "last_visit_date": "Last Visit Date",
    "days_since_last_visit": "Days Since Last Visit",
    "next_visit_due": "Next Visit Due",
    # USG Status
    "pregnant_woman_name": "Patient Name",
    "usg_centre_name": "USG Centre",
    "scheduled_date": "Scheduled Date",
    "status": "Status",
    "appointment_type": "Appointment Type",
    "days_pending": "Days Pending",
    "is_overdue": "Overdue",
    "completed_date": "Completed Date",
    # User Activity
    "user_name": "User Name",
    "role": "Role",
    "registrations": "Registrations",
    "total_actions": "Total Actions",
    # Comparative Analysis
    "name": "Name",
    "usg_completed": "USG Completed",
    "performance_score": "Performance Score",
    # Delivery Reporting
    "delivery_referrals": "Delivery Referrals",
    "completed_deliveries": "Completed Deliveries",
    "total_referrals": "Total Referrals",
    "pending_referrals": "Pending Referrals",
    "accepted_referrals": "Accepted Referrals",
    "safe_delivery": "Safe Delivery",
    "live_birth": "Live Birth",
    "still_birth": "Still Birth",
    "infant_death": "Infant Death",
    "maternal_death": "Maternal Death",
    "adverse_outcome_rate": "Adverse Outcome Rate (%)",
    "dp_name": "Delivery Point",
    "acceptance_rate": "Acceptance Rate (%)",
    "re_referral_rate": "Re-referral Rate (%)",
    "avg_acceptance_time_hours": "Avg Acceptance Time (Hours)",
    "total_delivery_referrals": "Total Delivery Referrals",
    "total_completed_deliveries": "Total Completed Deliveries",
}


def rename_columns(data: list) -> list:
    """Rename dict keys using COLUMN_LABELS mapping"""
    return [
        {COLUMN_LABELS.get(k, k.replace('_', ' ').title()): v for k, v in row.items()}
        for row in data
    ]


# Helper function to generate Excel file
async def generate_excel_report(data: dict, filename: str) -> StreamingResponse:
    """Generate Excel file from data dictionary with friendly column names"""
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for sheet_name, sheet_data in data.items():
            friendly_data = rename_columns(sheet_data) if sheet_data else sheet_data
            df = pd.DataFrame(friendly_data)
            df.to_excel(writer, sheet_name=sheet_name, index=False)

    output.seek(0)

    return StreamingResponse(
        io.BytesIO(output.read()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# Helper function to generate CSV file
async def generate_csv_report(data: list, filename: str) -> StreamingResponse:
    """Generate CSV file from data list with friendly column names"""
    df = pd.DataFrame(rename_columns(data) if data else data)
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)
    
    return StreamingResponse(
        io.StringIO(output.getvalue()),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

def _dt_between(col, start: date, end: date):
    """
    Inclusive *calendar-day* range on a DATETIME column.

    `col.between(date_a, date_b)` silently drops the whole of `date_b` (a bare
    date is midnight, so anything after 00:00:00 on the end date is excluded -
    including every record created "today").  This helper uses
    `col >= start 00:00` and `col < (end + 1 day) 00:00` instead.
    """
    return and_(
        col >= datetime.combine(start, datetime.min.time()),
        col < datetime.combine(end + timedelta(days=1), datetime.min.time()),
    )


def _enum_key(v) -> str:
    """Normalise an Enum / raw DB string (MySQL may hand back NAME or value) to a lowercase value."""
    if v is None:
        return "unknown"
    return (v.value if hasattr(v, "value") else str(v).split(".")[-1]).lower()


def _user_district_id(db: Session, user: User) -> Optional[int]:
    """District of any user, resolved through whichever jurisdiction they hold."""
    if user.district_id is not None:
        return user.district_id
    if user.block_id:
        blk = db.query(Block).filter(Block.id == user.block_id).first()
        if blk:
            return blk.district_id
    if user.sub_centre_id:
        sc = db.query(SubCentre).filter(SubCentre.id == user.sub_centre_id).first()
        blk = db.query(Block).filter(Block.id == sc.block_id).first() if sc else None
        if blk:
            return blk.district_id
    for model, attr in ((USGCentre, "usg_centre_id"), (DeliveryPoint, "dp_id"), (PMSMACentre, "pmsma_centre_id")):
        ref = getattr(user, attr, None)
        if ref:
            row = db.query(model.district_id).filter(model.id == ref).first()
            if row and row[0]:
                return row[0]
    return None


# ─── Delivery Helper Functions ───────────────────────────────────────────────

def _build_delivery_role_filter(query, current_user, referral_model=True):
    """
    Apply the role-based jurisdiction to a DeliveryReferral / DeliveryOutcome query.

    PregnantWoman is ALWAYS joined here, so callers can add block / sub-centre /
    ward / risk filters on PregnantWoman afterwards without joining again.
    Fails closed for any role that is not explicitly handled.
    """
    model = DeliveryReferral if referral_model else DeliveryOutcome
    role = current_user.role
    query = query.join(PregnantWoman, model.pregnant_woman_id == PregnantWoman.id)

    if role in ("district", "block", "sub_centre"):
        # scope_conditions() is ward-mapping aware for sub-centres and fails closed
        return query.filter(*scope_conditions(current_user))
    if role == "dp":
        if current_user.dp_id is None:
            return query.filter(false())
        return query.filter(model.dp_id == current_user.dp_id)
    if role == "pmsma":
        # PMSMA sees only referrals it created itself (mirrors dashboard scoping)
        if referral_model:
            return query.filter(DeliveryReferral.referred_by_user_id == current_user.id)
        own_pw = select(DeliveryReferral.pregnant_woman_id).where(
            DeliveryReferral.referred_by_user_id == current_user.id
        )
        return query.filter(DeliveryOutcome.pregnant_woman_id.in_(own_pw))
    return query.filter(false())


def _delivery_extra_filters(query, model, f: Optional[PWFilters] = None, dp_id: Optional[int] = None):
    """Beneficiary cohort filters (block / sub-centre / ward / risk / age ...) plus an optional delivery point."""
    if f is not None:
        conds = pw_conditions(f, None, include_scope=False)
        if conds:
            query = query.filter(*conds)
    if dp_id:
        query = query.filter(model.dp_id == dp_id)
    return query


def get_delivery_summary_data(
    db: Session,
    current_user,
    start_date: date,
    end_date: date,
    block_id: Optional[int] = None,
    f: Optional[PWFilters] = None,
    dp_id: Optional[int] = None,
) -> dict:
    """
    Aggregated delivery summary for a given user scope and (inclusive) date range.

    Referrals are dated by the day they were raised, outcomes by delivery date.
    Errors are NOT swallowed here: a broken query must surface as an error, not
    as a report full of zeros.
    """
    ref_q = db.query(DeliveryReferral.status, func.count(DeliveryReferral.id).label("cnt"))
    ref_q = _build_delivery_role_filter(ref_q, current_user, referral_model=True)
    ref_q = _delivery_extra_filters(ref_q, DeliveryReferral, f, dp_id)
    ref_q = ref_q.filter(_dt_between(DeliveryReferral.created_at, start_date, end_date))
    if block_id:
        ref_q = ref_q.filter(PregnantWoman.block_id == block_id)
    status_dict: Dict[str, int] = {}
    for s, c in ref_q.group_by(DeliveryReferral.status).all():
        status_dict[_enum_key(s)] = int(c or 0)

    out_q = db.query(DeliveryOutcome.delivery_type, func.count(DeliveryOutcome.id).label("cnt"))
    out_q = _build_delivery_role_filter(out_q, current_user, referral_model=False)
    out_q = _delivery_extra_filters(out_q, DeliveryOutcome, f, dp_id)
    out_q = out_q.filter(_dt_between(DeliveryOutcome.delivery_date, start_date, end_date))
    if block_id:
        out_q = out_q.filter(PregnantWoman.block_id == block_id)
    outcome_dict: Dict[str, int] = {}
    for dt, c in out_q.group_by(DeliveryOutcome.delivery_type).all():
        outcome_dict[_enum_key(dt)] = int(c or 0)

    total_outcomes = sum(outcome_dict.values())
    adverse = (
        outcome_dict.get("still_birth", 0)
        + outcome_dict.get("infant_death", 0)
        + outcome_dict.get("maternal_death", 0)
    )
    adverse_rate = round((adverse / total_outcomes * 100) if total_outcomes > 0 else 0, 2)

    return {
        "total_referrals": sum(status_dict.values()),
        "pending": status_dict.get("pending", 0),
        "accepted": status_dict.get("accepted", 0),
        "re_referred": status_dict.get("re_referred", 0),
        "completed": status_dict.get("completed", 0),
        "outcomes": outcome_dict,
        "total_outcomes": total_outcomes,
        "adverse_rate": adverse_rate,
    }


def get_delivery_counts_by_ward(
    db: Session,
    ward_id: int,
    start_date: date,
    end_date: date
) -> tuple:
    """
    Returns (referral_count, completed_delivery_count) for a single ward over an
    inclusive date range.  Aggregated SQL, no Python loops.
    """
    referral_count = (
        db.query(func.count(DeliveryReferral.id))
        .join(PregnantWoman, DeliveryReferral.pregnant_woman_id == PregnantWoman.id)
        .filter(
            PregnantWoman.ward_id == ward_id,
            _dt_between(DeliveryReferral.created_at, start_date, end_date),
        )
        .scalar() or 0
    )
    outcome_count = (
        db.query(func.count(DeliveryOutcome.id))
        .join(PregnantWoman, DeliveryOutcome.pregnant_woman_id == PregnantWoman.id)
        .filter(
            PregnantWoman.ward_id == ward_id,
            _dt_between(DeliveryOutcome.delivery_date, start_date, end_date),
        )
        .scalar() or 0
    )
    return (referral_count, outcome_count)


# ─── End Delivery Helper Functions ───────────────────────────────────────────


# DISTRICT USER REPORTS
@router.get("/district/performance")
async def get_district_performance_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    block_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get district performance report (District users only)"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="District user access required")
    
    # Set default date range if not provided
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=30)
    
    # Base query for district data with date filter
    query = db.query(PregnantWoman).filter(
        PregnantWoman.district_id == current_user.district_id,
        _dt_between(PregnantWoman.created_at, start_date, end_date)
    )
    
    if block_id:
        query = query.filter(PregnantWoman.block_id == block_id)
    
    # Get district statistics
    total_pregnant_women = query.count()
    high_risk_cases = query.filter(PregnantWoman.is_high_risk == True).count()
    self_registered = query.filter(PregnantWoman.is_self_registered == True).count()
    
    # Get block-wise breakdown with date filter
    block_stats_query = db.query(
        Block.id.label('block_id'),
        Block.name.label('block_name'),
        func.count(PregnantWoman.id).label('total_women'),
        func.sum(case((PregnantWoman.is_high_risk == True, 1), else_=0)).label('high_risk'),
        func.sum(case((PregnantWoman.is_self_registered == True, 1), else_=0)).label('self_registered')
    ).join(PregnantWoman, Block.id == PregnantWoman.block_id)\
     .filter(
         Block.district_id == current_user.district_id,
         _dt_between(PregnantWoman.created_at, start_date, end_date)
     )
    
    if block_id:
        block_stats_query = block_stats_query.filter(Block.id == block_id)
    
    block_stats = block_stats_query.group_by(Block.id, Block.name).all()
    
    # Get ANC visits and USG referrals for each block
    block_wise_data = []
    for stat in block_stats:
        anc_visits = db.query(func.count(ANCVisit.id)).join(PregnantWoman)\
            .filter(
                PregnantWoman.block_id == stat.block_id,
                ANCVisit.visit_date.between(start_date, end_date)
            ).scalar() or 0
        
        usg_referrals = db.query(func.count(ANCVisit.id)).join(PregnantWoman)\
            .filter(
                PregnantWoman.block_id == stat.block_id,
                ANCVisit.referred_for_usg == True,
                ANCVisit.visit_date.between(start_date, end_date)
            ).scalar() or 0
        
        block_wise_data.append({
            "block_name": stat.block_name,
            "total_women": stat.total_women,
            "high_risk": stat.high_risk,
            "self_registered": stat.self_registered,
            "anc_visits": anc_visits,
            "usg_referrals": usg_referrals
        })
    
    # Get USG appointment statistics (date-filtered)
    usg_stats_query = db.query(
        func.count(USGAppointment.id).label('total_appointments'),
        func.sum(case((USGAppointment.status == 'completed', 1), else_=0)).label('completed'),
        func.sum(case((USGAppointment.appointment_type == 'emergency', 1), else_=0)).label('emergency')
    ).join(PregnantWoman, USGAppointment.pregnant_woman_id == PregnantWoman.id)\
     .filter(PregnantWoman.district_id == current_user.district_id)\
     .filter(_dt_between(USGAppointment.scheduled_date, start_date, end_date))
    
    if block_id:
        usg_stats_query = usg_stats_query.filter(PregnantWoman.block_id == block_id)
    
    usg_stats = usg_stats_query.first()
    
    # Get grievance statistics (date-filtered)
    grievance_stats_query = db.query(
        func.count(Grievance.id).label('total_grievances'),
        func.sum(case((Grievance.status == 'resolved', 1), else_=0)).label('resolved'),
        func.sum(case((Grievance.escalated_to_district == True, 1), else_=0)).label('escalated')
    ).filter(Grievance.district_id == current_user.district_id)\
     .filter(_dt_between(Grievance.created_at, start_date, end_date))
    
    if block_id:
        grievance_stats_query = grievance_stats_query.filter(Grievance.assigned_to_block == block_id)
    
    grievance_stats = grievance_stats_query.first()
    
    response = {
        "report_period": {
            "start_date": start_date,
            "end_date": end_date
        },
        "district_summary": {
            "total_pregnant_women": total_pregnant_women,
            "high_risk_cases": high_risk_cases,
            "self_registered": self_registered,
            "high_risk_percentage": round((high_risk_cases / total_pregnant_women * 100) if total_pregnant_women > 0 else 0, 2)
        },
        "block_wise_data": block_wise_data,
        "usg_statistics": {
            "total_appointments": usg_stats.total_appointments or 0,
            "completed_appointments": usg_stats.completed or 0,
            "emergency_appointments": usg_stats.emergency or 0,
            "completion_rate": round((usg_stats.completed / usg_stats.total_appointments * 100) if usg_stats.total_appointments > 0 else 0, 2)
        },
        "grievance_statistics": {
            "total_grievances": grievance_stats.total_grievances or 0,
            "resolved_grievances": grievance_stats.resolved or 0,
            "escalated_grievances": grievance_stats.escalated or 0,
            "resolution_rate": round((grievance_stats.resolved / grievance_stats.total_grievances * 100) if grievance_stats.total_grievances > 0 else 0, 2)
        }
    }

    # --- delivery_statistics: NEW additive key (Step 5A) ---
    try:
        d = get_delivery_summary_data(db, current_user, start_date, end_date, block_id)
        response["delivery_statistics"] = {
            "total_referrals": d["total_referrals"],
            "completed_deliveries": d["completed"],
            "pending_referrals": d["pending"],
            "outcome_breakdown": {
                "safe_delivery":   d["outcomes"].get("safe_delivery", 0),
                "live_birth":      d["outcomes"].get("live_birth", 0),
                "still_birth":     d["outcomes"].get("still_birth", 0),
                "infant_death":    d["outcomes"].get("infant_death", 0),
                "maternal_death":  d["outcomes"].get("maternal_death", 0),
            },
            "adverse_outcome_rate": d["adverse_rate"],
        }
    except Exception:
        pass  # fail silently — never break existing response

    return response


def _get_block_detail_data(db: Session, block_id: int, start_date: date, end_date: date) -> dict:
    """Fetch ward summary and pregnant women list for a single block."""
    # --- Ward summary: all wards in block, with counts for women registered in date range ---
    wards = db.query(Ward).filter(Ward.block_id == block_id).all()

    ward_rows = []
    for ward in wards:
        total_women = db.query(func.count(PregnantWoman.id)).filter(
            PregnantWoman.ward_id == ward.id,
            _dt_between(PregnantWoman.created_at, start_date, end_date)
        ).scalar() or 0

        high_risk = db.query(func.count(PregnantWoman.id)).filter(
            PregnantWoman.ward_id == ward.id,
            PregnantWoman.is_high_risk == True,
            _dt_between(PregnantWoman.created_at, start_date, end_date)
        ).scalar() or 0

        anc_visits = db.query(func.count(ANCVisit.id)).join(PregnantWoman).filter(
            PregnantWoman.ward_id == ward.id,
            ANCVisit.visit_date.between(start_date, end_date)
        ).scalar() or 0

        usg_referrals = db.query(func.count(ANCVisit.id)).join(PregnantWoman).filter(
            PregnantWoman.ward_id == ward.id,
            ANCVisit.referred_for_usg == True,
            ANCVisit.visit_date.between(start_date, end_date)
        ).scalar() or 0

        emergency_visits = db.query(func.count(ANCVisit.id)).join(PregnantWoman).filter(
            PregnantWoman.ward_id == ward.id,
            ANCVisit.is_emergency == True,
            ANCVisit.visit_date.between(start_date, end_date)
        ).scalar() or 0

        ward_rows.append({
            "ward_name": ward.name,
            "total_women": total_women,
            "high_risk": high_risk,
            "anc_visits": anc_visits,
            "usg_referrals": usg_referrals,
            "emergency_visits": emergency_visits
        })

    # --- Pregnant women detail: ALL active women in block (not date-filtered) ---
    women = db.query(PregnantWoman).filter(
        PregnantWoman.block_id == block_id,
        PregnantWoman.is_active == True
    ).order_by(PregnantWoman.full_name).all()

    women_rows = []
    for pw in women:
        anc_count = db.query(func.count(ANCVisit.id)).filter(
            ANCVisit.pregnant_woman_id == pw.id
        ).scalar() or 0

        usg_count = db.query(func.count(USGAppointment.id)).filter(
            USGAppointment.pregnant_woman_id == pw.id,
            USGAppointment.status == "completed"
        ).scalar() or 0

        ward_name = None
        if pw.ward_id:
            ward = db.query(Ward).filter(Ward.id == pw.ward_id).first()
            ward_name = ward.name if ward else None

        sub_centre_name = None
        if pw.sub_centre_id:
            sc = db.query(SubCentre).filter(SubCentre.id == pw.sub_centre_id).first()
            sub_centre_name = sc.name if sc else None

        women_rows.append({
            "full_name": pw.full_name,
            "mobile_number": pw.mobile_number,
            "age": pw.age,
            "high_risk": "Yes" if pw.is_high_risk else "No",
            "risk_factors": pw.risk_factors or "",
            "lmp_date": pw.lmp_date,
            "edd_date": pw.edd_date,
            "anc_visits": anc_count,
            "usg_completed": usg_count,
            "registration_status": "Approved" if pw.registration_approved else "Pending",
            "ward_name": ward_name or "",
            "sub_centre_name": sub_centre_name or ""
        })

    return {"ward_rows": ward_rows, "women_rows": women_rows}


@router.get("/district/export/{format}")
async def export_district_report(
    format: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    block_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Export district report in specified format"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="District user access required")

    if format not in ["excel", "csv"]:
        raise HTTPException(status_code=400, detail="Format must be 'excel' or 'csv'")

    # Set default date range
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=30)

    # Get report data
    report_data = await get_district_performance_report(start_date, end_date, block_id, db, current_user)

    if format == "excel":
        output = io.BytesIO()

        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            # --- Sheet 1: District Summary ---
            pd.DataFrame(rename_columns([report_data["district_summary"]])).to_excel(
                writer, sheet_name="District Summary", index=False
            )

            # --- Sheet 2: Block Wise Data ---
            pd.DataFrame(rename_columns(report_data["block_wise_data"])).to_excel(
                writer, sheet_name="Block Wise Data", index=False
            )

            # --- Sheet 3: USG Statistics ---
            pd.DataFrame(rename_columns([report_data["usg_statistics"]])).to_excel(
                writer, sheet_name="USG Statistics", index=False
            )

            # --- Sheet 4: Grievance Statistics ---
            pd.DataFrame(rename_columns([report_data["grievance_statistics"]])).to_excel(
                writer, sheet_name="Grievance Statistics", index=False
            )

            # --- Sheet 5: Delivery Statistics (NEW — Step 5C) ---
            try:
                d = get_delivery_summary_data(db, current_user, start_date, end_date, block_id)

                # Row 1: district-level summary
                summary_row = [{
                    "total_referrals":      d["total_referrals"],
                    "pending_referrals":    d["pending"],
                    "accepted_referrals":   d["accepted"],
                    "completed_deliveries": d["completed"],
                    "safe_delivery":        d["outcomes"].get("safe_delivery", 0),
                    "live_birth":           d["outcomes"].get("live_birth", 0),
                    "still_birth":          d["outcomes"].get("still_birth", 0),
                    "infant_death":         d["outcomes"].get("infant_death", 0),
                    "maternal_death":       d["outcomes"].get("maternal_death", 0),
                    "adverse_outcome_rate": d["adverse_rate"],
                }]
                summary_df = pd.DataFrame(rename_columns(summary_row))
                summary_df.to_excel(writer, sheet_name="Delivery Statistics", index=False, startrow=0)

                # Rows below: one row per Delivery Point
                dp_q = db.query(DeliveryPoint).filter(
                    DeliveryPoint.is_active == True,
                    DeliveryPoint.district_id == current_user.district_id
                ).all()

                # Aggregated referral counts per DP
                ref_agg = db.query(
                    DeliveryReferral.dp_id,
                    DeliveryReferral.status,
                    func.count(DeliveryReferral.id).label("cnt")
                ).filter(
                    _dt_between(DeliveryReferral.created_at, start_date, end_date)
                ).group_by(DeliveryReferral.dp_id, DeliveryReferral.status).all()

                # Aggregated outcome counts per DP
                out_agg = db.query(
                    DeliveryOutcome.dp_id,
                    DeliveryOutcome.delivery_type,
                    func.count(DeliveryOutcome.id).label("cnt")
                ).filter(
                    DeliveryOutcome.delivery_date >= start_date,
                    DeliveryOutcome.delivery_date < end_date + timedelta(days=1)
                ).group_by(DeliveryOutcome.dp_id, DeliveryOutcome.delivery_type).all()

                ref_by_dp = {}
                for row in ref_agg:
                    if row.dp_id not in ref_by_dp:
                        ref_by_dp[row.dp_id] = {}
                    key = row.status.value.lower() if hasattr(row.status, 'value') else str(row.status).split('.')[-1].lower()
                    ref_by_dp[row.dp_id][key] = row.cnt

                out_by_dp = {}
                for row in out_agg:
                    if row.dp_id not in out_by_dp:
                        out_by_dp[row.dp_id] = {}
                    key = row.delivery_type.value.lower() if hasattr(row.delivery_type, 'value') else str(row.delivery_type).split('.')[-1].lower()
                    out_by_dp[row.dp_id][key] = row.cnt

                dp_rows = []
                for dp in dp_q:
                    ref = ref_by_dp.get(dp.id, {})
                    out = out_by_dp.get(dp.id, {})
                    total_ref = sum(ref.values())
                    dp_rows.append({
                        "Delivery Point": dp.name,
                        "Total Referrals": total_ref,
                        "Pending Referrals": ref.get("pending", 0),
                        "Accepted Referrals": ref.get("accepted", 0) + ref.get("completed", 0),
                        "Completed Deliveries": ref.get("completed", 0),
                        "Safe Delivery": out.get("safe_delivery", 0),
                        "Live Birth": out.get("live_birth", 0),
                        "Still Birth": out.get("still_birth", 0),
                        "Infant Death": out.get("infant_death", 0),
                        "Maternal Death": out.get("maternal_death", 0),
                    })

                if dp_rows:
                    from openpyxl.styles import Font as OFont
                    ws = writer.sheets["Delivery Statistics"]
                    # Gap row then DP header label
                    gap_row = len(summary_df) + 3  # +1 header +1 data +1 blank
                    ws.cell(row=gap_row, column=1, value="Delivery Point Wise Breakdown").font = OFont(bold=True)
                    pd.DataFrame(dp_rows).to_excel(
                        writer, sheet_name="Delivery Statistics", index=False, startrow=gap_row
                    )

            except Exception:
                pass  # fail silently — never break existing sheets

            # --- Per-block sheets: Ward Summary + Women Detail ---
            blocks_to_export = db.query(Block).filter(
                Block.district_id == current_user.district_id,
                Block.is_active == True
            )
            if block_id:
                blocks_to_export = blocks_to_export.filter(Block.id == block_id)
            blocks_to_export = blocks_to_export.all()

            from openpyxl.styles import Font

            for block in blocks_to_export:
                detail = _get_block_detail_data(db, block.id, start_date, end_date)

                # Excel sheet name max 31 chars
                sheet_name = block.name[:31]

                ward_rows = detail["ward_rows"]
                women_rows = detail["women_rows"]

                # Always write ward summary (even if empty, use placeholder)
                ward_df = pd.DataFrame(rename_columns(ward_rows)) if ward_rows else pd.DataFrame([{"Info": "No wards found for this block"}])
                women_df = pd.DataFrame(rename_columns(women_rows)) if women_rows else pd.DataFrame([{"Info": "No active pregnant women found for this block"}])

                # Section 1: Ward Summary header label at row 0, table at row 1
                ward_df.to_excel(writer, sheet_name=sheet_name, index=False, startrow=1)

                worksheet = writer.sheets[sheet_name]

                # Bold section label for ward summary
                ward_label = worksheet.cell(row=1, column=1, value="Ward Summary")
                ward_label.font = Font(bold=True)

                # Section 2: Women detail — starts after ward table + 2 gap rows
                women_start_row = 1 + len(ward_df) + 3

                # Bold section label for women detail
                women_label = worksheet.cell(row=women_start_row, column=1, value="Pregnant Women Detail")
                women_label.font = Font(bold=True)

                women_df.to_excel(writer, sheet_name=sheet_name, index=False, startrow=women_start_row + 1)

        output.seek(0)
        filename = f"district_report_{start_date}_{end_date}.xlsx"
        return StreamingResponse(
            io.BytesIO(output.read()),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    elif format == "csv":
        filename = f"district_block_data_{start_date}_{end_date}.csv"
        return await generate_csv_report(report_data["block_wise_data"], filename)

# BLOCK USER REPORTS
@router.get("/block/ward-wise")
async def get_ward_wise_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    ward_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get ward-wise report for block users"""
    if current_user.role != "block":
        raise HTTPException(status_code=403, detail="Block user access required")
    
    # Set default date range
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=30)
    
    # Base ward_stats query with date filter and ward_id filter
    ward_stats_query = db.query(
        Ward.id.label('ward_id'),
        func.coalesce(Ward.name, 'Rural Area (No Ward)').label('ward_name'),
        func.count(PregnantWoman.id).label('total_women'),
        func.sum(case((PregnantWoman.is_high_risk == True, 1), else_=0)).label('high_risk'),
        func.sum(case((PregnantWoman.registration_approved == True, 1), else_=0)).label('approved'),
        func.sum(case((PregnantWoman.is_self_registered == True, 1), else_=0)).label('self_registered')
    ).outerjoin(Ward, Ward.id == PregnantWoman.ward_id)\
     .filter(
         PregnantWoman.block_id == current_user.block_id,
         _dt_between(PregnantWoman.created_at, start_date, end_date)
     )

    if ward_id:
        ward_stats_query = ward_stats_query.filter(PregnantWoman.ward_id == ward_id)

    ward_stats = ward_stats_query.group_by(Ward.id, Ward.name).all()

    # Get ANC visit statistics by ward with date filter and ward_id filter
    anc_stats_query = db.query(
        Ward.id.label('ward_id'),
        func.coalesce(Ward.name, 'Rural Area (No Ward)').label('ward_name'),
        func.count(ANCVisit.id).label('total_visits'),
        func.sum(case((ANCVisit.referred_for_usg == True, 1), else_=0)).label('usg_referrals'),
        func.sum(case((ANCVisit.is_emergency == True, 1), else_=0)).label('emergency_visits')
    ).join(PregnantWoman, ANCVisit.pregnant_woman_id == PregnantWoman.id)\
     .outerjoin(Ward, PregnantWoman.ward_id == Ward.id)\
     .filter(
         PregnantWoman.block_id == current_user.block_id,
         ANCVisit.visit_date.between(start_date, end_date)
     )

    if ward_id:
        anc_stats_query = anc_stats_query.filter(PregnantWoman.ward_id == ward_id)

    anc_stats = anc_stats_query.group_by(Ward.id, Ward.name).all()
    
    # Combine ward statistics — match by ward_id (not name, to avoid duplicates)
    ward_data = []
    for ward_stat in ward_stats:
        anc_data = next((anc for anc in anc_stats if anc.ward_id == ward_stat.ward_id), None)

        # Step 5B: delivery counts per ward (safe, fail silently)
        try:
            dr_count, cd_count = get_delivery_counts_by_ward(
                db, ward_stat.ward_id, start_date, end_date
            )
        except Exception:
            dr_count, cd_count = 0, 0

        ward_data.append({
            "ward_name": ward_stat.ward_name,
            "total_pregnant_women": ward_stat.total_women,
            "high_risk_cases": ward_stat.high_risk,
            "approved_registrations": ward_stat.approved,
            "self_registrations": ward_stat.self_registered,
            "total_anc_visits": anc_data.total_visits if anc_data else 0,
            "usg_referrals": anc_data.usg_referrals if anc_data else 0,
            "emergency_visits": anc_data.emergency_visits if anc_data else 0,
            "delivery_referrals": dr_count,
            "completed_deliveries": cd_count,
        })

    return {
        "report_period": {
            "start_date": start_date,
            "end_date": end_date
        },
        "block_summary": {
            "total_wards": len(ward_data),
            "total_pregnant_women": sum(w["total_pregnant_women"] for w in ward_data),
            "total_high_risk": sum(w["high_risk_cases"] for w in ward_data),
            "total_anc_visits": sum(w["total_anc_visits"] for w in ward_data),
            "total_delivery_referrals": sum(w["delivery_referrals"] for w in ward_data),
            "total_completed_deliveries": sum(w["completed_deliveries"] for w in ward_data),
        },
        "ward_wise_data": ward_data
    }

@router.get("/block/export/{format}")
async def export_block_report(
    format: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    ward_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Export block ward-wise report"""
    if current_user.role != "block":
        raise HTTPException(status_code=403, detail="Block user access required")
    
    if format not in ["excel", "csv"]:
        raise HTTPException(status_code=400, detail="Format must be 'excel' or 'csv'")
    
    # Get report data
    report_data = await get_ward_wise_report(start_date, end_date, ward_id, db, current_user)
    
    if format == "excel":
        # Step 5D: block_summary already has total_delivery_referrals and
        # total_completed_deliveries from get_ward_wise_report.
        # ward_wise_data already has delivery_referrals and completed_deliveries.
        # rename_columns inside generate_excel_report handles friendly labels.
        excel_data = {
            "Block Summary": [report_data["block_summary"]],
            "Ward Wise Data": report_data["ward_wise_data"]
        }
        filename = f"block_ward_report_{start_date or 'all'}_{end_date or 'all'}.xlsx"
        return await generate_excel_report(excel_data, filename)

    elif format == "csv":
        filename = f"ward_wise_data_{start_date or 'all'}_{end_date or 'all'}.csv"
        return await generate_csv_report(report_data["ward_wise_data"], filename)

# ============================================
# PHASE 1: ESSENTIAL REPORTS  (advanced filtering)
# ============================================
#
# The three list reports below (High-Risk Tracking, ANC Compliance, USG Status)
# share one filter vocabulary with the District dashboard (see analytics_filters.py):
# block / sub-centre / ward, risk, age band, trimester, parity, blood group,
# registration source, risk factors, anaemia, ... plus report-specific filters.
#
# Everything is filtered, sorted, searched and paginated in SQL, so page N of a
# filtered report is always consistent, summary cards describe the *whole*
# filtered result (not just the visible page), and the Excel/CSV export contains
# exactly the rows the user is looking at.

MAX_EXPORT_ROWS = 20000
OPEN_USG_STATUSES = (AppointmentStatus.SCHEDULED, AppointmentStatus.ACCEPTED, AppointmentStatus.RESCHEDULED)

hr_filters_dep = make_pw_filters_dep("active")
anc_filters_dep = make_pw_filters_dep("active")
usg_filters_dep = make_pw_filters_dep("all")


def _day_start(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


def _day_end(d: date) -> datetime:
    """Exclusive upper bound covering the whole of day `d`."""
    return datetime.combine(d + timedelta(days=1), datetime.min.time())


def _search_cond(term: Optional[str], cols):
    term = (term or "").strip()
    if not term:
        return None
    return or_(*[c.ilike(f"%{term}%") for c in cols])


def _order_by(sort_map: dict, sort_by: Optional[str], sort_dir: str, default: str):
    col = sort_map.get(sort_by or default, sort_map[default])
    primary = col.desc() if sort_dir == "desc" else col.asc()
    return [primary, PregnantWoman.id.asc()]   # id makes pagination order deterministic


def _person_place(pw: PregnantWoman) -> dict:
    weeks = gestational_weeks(pw.lmp_date, pw.edd_date)
    return {
        "age": pw.age,
        "blood_group": pw.blood_group,
        "gravida": pw.gravida,
        "gestational_weeks": weeks,
        "trimester": trimester_of(weeks) if pw.is_active else None,
        "is_self_registered": bool(pw.is_self_registered),
        "is_high_risk": bool(pw.is_high_risk),
    }


def _group_counts(db: Session, query_factory, group_col, names_model=None):
    """Counts per group for a factory returning a fresh filtered query on `select(...)`."""
    rows = query_factory(db.query(group_col, func.count(PregnantWoman.id))).group_by(group_col).all()
    names = {}
    if names_model is not None:
        ids = [r[0] for r in rows if r[0] is not None]
        if ids:
            names = {r.id: r.name for r in db.query(names_model.id, names_model.name).filter(names_model.id.in_(ids)).all()}
    return [{"id": r[0], "name": names.get(r[0], "Unassigned") if names_model else str(r[0]), "count": int(r[1] or 0)}
            for r in rows]


def _place_lookup(db: Session, pws):
    blocks = {b.id: b.name for b in db.query(Block.id, Block.name).filter(Block.id.in_({p.block_id for p in pws if p.block_id})).all()} if pws else {}
    subs = {s.id: s.name for s in db.query(SubCentre.id, SubCentre.name).filter(SubCentre.id.in_({p.sub_centre_id for p in pws if p.sub_centre_id})).all()} if pws else {}
    wards = {w.id: w.name for w in db.query(Ward.id, Ward.name).filter(Ward.id.in_({p.ward_id for p in pws if p.ward_id})).all()} if pws else {}
    return blocks, subs, wards


def _pager(total: int, page: int, page_size: int) -> dict:
    return {"page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size if page_size else 1,
            "total_count": total}


# ── Filter options for the Reports filter panel ──────────────────────────────

@router.get("/filter-options")
async def get_report_filter_options(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Dropdown values for the report filters, limited to what the user may see (every role)."""
    role = current_user.role
    district_id = _user_district_id(db, current_user)

    blocks, subs, wards = [], [], []
    if role == "district" and current_user.district_id:
        blocks = db.query(Block).filter(Block.district_id == current_user.district_id, Block.is_active == True).order_by(Block.name).all()  # noqa: E712
        bids = [b.id for b in blocks]
        if bids:
            subs = db.query(SubCentre).filter(SubCentre.block_id.in_(bids), SubCentre.is_active == True).order_by(SubCentre.name).all()  # noqa: E712
            wards = db.query(Ward).filter(Ward.block_id.in_(bids), Ward.is_active == True).order_by(Ward.name).all()  # noqa: E712
    elif role in ("block", "pmsma") and current_user.block_id:
        subs = db.query(SubCentre).filter(SubCentre.block_id == current_user.block_id, SubCentre.is_active == True).order_by(SubCentre.name).all()  # noqa: E712
        wards = db.query(Ward).filter(Ward.block_id == current_user.block_id, Ward.is_active == True).order_by(Ward.name).all()  # noqa: E712
    elif role == "sub_centre" and current_user.sub_centre_id:
        subs = db.query(SubCentre).filter(SubCentre.id == current_user.sub_centre_id).all()
        wards = db.query(Ward).filter(Ward.id.in_(
            db.query(WardSubcentreMapping.ward_id).filter(WardSubcentreMapping.sub_centre_id == current_user.sub_centre_id)
        )).order_by(Ward.name).all()

    usg_q = db.query(USGCentre).filter(USGCentre.is_active == True)  # noqa: E712
    if role == "usg_centre":
        usg_q = usg_q.filter(USGCentre.id == current_user.usg_centre_id) if current_user.usg_centre_id else usg_q.filter(false())
    elif district_id:
        usg_q = usg_q.filter(USGCentre.district_id == district_id)
    else:
        usg_q = usg_q.filter(false())
    usg_centres = usg_q.order_by(USGCentre.name).all()

    dp_q = db.query(DeliveryPoint).filter(DeliveryPoint.is_active == True)  # noqa: E712
    if role == "dp":
        dp_q = dp_q.filter(DeliveryPoint.id == current_user.dp_id) if current_user.dp_id else dp_q.filter(false())
    elif district_id:
        dp_q = dp_q.filter(DeliveryPoint.district_id == district_id)
    else:
        dp_q = dp_q.filter(false())
    dps = dp_q.order_by(DeliveryPoint.name).all()

    pm_q = db.query(PMSMACentre).filter(PMSMACentre.is_active == True)  # noqa: E712
    if district_id:
        pm_q = pm_q.filter(PMSMACentre.district_id == district_id)
    else:
        pm_q = pm_q.filter(false())
    if role == "pmsma" and current_user.pmsma_centre_id:
        pm_q = pm_q.filter(PMSMACentre.id == current_user.pmsma_centre_id)
    pmsma_centres = pm_q.order_by(PMSMACentre.name).all()

    scope = report_scope_conditions(current_user)
    blood = sorted({r[0] for r in db.query(PregnantWoman.blood_group).filter(*scope, PregnantWoman.blood_group.isnot(None), PregnantWoman.blood_group != "").distinct().all()})
    counter: Dict[str, int] = {}
    for (text,) in db.query(PregnantWoman.risk_factors).filter(*scope, PregnantWoman.risk_factors.isnot(None), PregnantWoman.risk_factors != "").all():
        for part in text.split(","):
            part = part.strip()
            if part:
                key = part.title() if part.islower() else part
                counter[key] = counter.get(key, 0) + 1
    return {
        "blocks": [{"id": b.id, "name": b.name} for b in blocks],
        "sub_centres": [{"id": s.id, "name": s.name, "block_id": s.block_id} for s in subs],
        "wards": [{"id": w.id, "name": w.name, "block_id": w.block_id} for w in wards],
        "usg_centres": [{"id": c.id, "name": c.name} for c in usg_centres],
        "delivery_points": [{"id": d.id, "name": d.name} for d in dps],
        "pmsma_centres": [{"id": c.id, "name": c.name} for c in pmsma_centres],
        "blood_groups": blood,
        "risk_factors": [{"name": k, "count": v} for k, v in sorted(counter.items(), key=lambda kv: -kv[1])[:40]],
        "age_bands": [{"key": k, "label": v} for k, v in AGE_BAND_LABELS.items()],
    }


# ── 1. HIGH-RISK PREGNANCY TRACKING REPORT ───────────────────────────────────

HR_SORTS = ("registered", "name", "age", "edd", "last_anc", "anc_visits", "usg_scans")


def _run_high_risk_report(db: Session, user: User, f: PWFilters, p: dict, page: int, page_size: Optional[int]):
    PW = PregnantWoman
    today = date.today()
    f.risk = "high"                                   # this report is high-risk by definition
    end = f.end_date or today
    start = f.start_date or (end - timedelta(days=90))

    anc = db.query(ANCVisit.pregnant_woman_id.label("pid"), func.count(ANCVisit.id).label("n"),
                   func.max(ANCVisit.visit_date).label("last")).group_by(ANCVisit.pregnant_woman_id).subquery()
    usg = db.query(USGAppointment.pregnant_woman_id.label("pid"), func.count(USGAppointment.id).label("n")) \
        .filter(USGAppointment.status == AppointmentStatus.COMPLETED).group_by(USGAppointment.pregnant_woman_id).subquery()
    anc_n, usg_n = func.coalesce(anc.c.n, 0), func.coalesce(usg.c.n, 0)
    fu_days = p["followup_days"]
    fu_cut = today - timedelta(days=fu_days)
    recent = and_(anc.c.last.isnot(None), anc.c.last >= fu_cut)
    attention = or_(anc.c.last.is_(None), anc.c.last < fu_cut)

    conds = pw_conditions(f, user)
    basis = p["date_basis"]
    if basis == "edd":
        conds += [PW.edd_date >= start, PW.edd_date <= end]
    elif basis != "any":
        conds += [PW.created_at >= _day_start(start), PW.created_at < _day_end(end)]
    if p["followup"] == "recent":
        conds.append(recent)
    elif p["followup"] == "attention":
        conds.append(attention)
    elif p["followup"] == "never":
        conds.append(anc.c.last.is_(None))
    if p["anc_visits_min"] is not None:
        conds.append(anc_n >= p["anc_visits_min"])
    if p["anc_visits_max"] is not None:
        conds.append(anc_n <= p["anc_visits_max"])
    if p["usg_done"] == "yes":
        conds.append(usg_n > 0)
    elif p["usg_done"] == "no":
        conds.append(usg_n == 0)
    if p["edd_within_days"]:
        conds += [PW.edd_date.isnot(None), PW.edd_date >= today, PW.edd_date <= today + timedelta(days=p["edd_within_days"])]
    sc = _search_cond(p["search"], [PW.full_name, PW.mobile_number, PW.rch_id, PW.abha_id])
    if sc is not None:
        conds.append(sc)

    def joined(q):
        return q.select_from(PW).outerjoin(anc, anc.c.pid == PW.id).outerjoin(usg, usg.c.pid == PW.id).filter(*conds)

    total = joined(db.query(func.count(PW.id))).scalar() or 0
    n_recent, n_attn, n_never, n_no_usg = joined(db.query(
        func.sum(case((recent, 1), else_=0)), func.sum(case((attention, 1), else_=0)),
        func.sum(case((anc.c.last.is_(None), 1), else_=0)), func.sum(case((usg_n == 0, 1), else_=0)))).one()

    # Risk-factor mix across the *whole* filtered set
    rf: Dict[str, list] = {}
    for (text,) in joined(db.query(PW.risk_factors)).filter(PW.risk_factors.isnot(None), PW.risk_factors != "").all():
        for part in {x.strip() for x in text.split(",") if x.strip()}:
            rf.setdefault(part.lower(), [part.title() if part.islower() else part, 0])[1] += 1
    risk_factors_count = {v[0]: v[1] for v in sorted(rf.values(), key=lambda v: -v[1])}

    # Breakdowns for the report charts
    by_block = _group_counts(db, joined, PW.block_id, Block) if user.role == "district" and not f.block_id else []
    by_sub = _group_counts(db, joined, PW.sub_centre_id, SubCentre) if (user.role == "block" or f.block_id) and not f.sub_centre_id else []
    ages: Dict[str, int] = {}
    for age, n in joined(db.query(PW.age, func.count(PW.id))).group_by(PW.age).all():
        key = "unknown"
        if age is not None:
            key = next((k for k, (lo, hi) in AGE_BANDS.items() if (lo is None or age >= lo) and (hi is None or age <= hi)), "unknown")
        ages[key] = ages.get(key, 0) + int(n or 0)
    by_age = [{"key": k, "name": AGE_BAND_LABELS[k], "count": ages[k]} for k in AGE_BAND_LABELS if ages.get(k)]
    by_tri = []
    for key, label in (("1", "1st trimester"), ("2", "2nd trimester"), ("3", "3rd trimester"), ("unknown", "Not recorded")):
        c = joined(db.query(func.count(PW.id))).filter(trimester_condition([key])).scalar() or 0
        if c:
            by_tri.append({"key": key, "name": label, "count": int(c)})

    sort_map = {"registered": PW.created_at, "name": PW.full_name, "age": PW.age, "edd": PW.edd_date,
                "last_anc": anc.c.last, "anc_visits": anc_n, "usg_scans": usg_n}
    q = joined(db.query(PW, anc.c.n, anc.c.last, usg.c.n)).order_by(*_order_by(sort_map, p["sort_by"], p["sort_dir"], "registered"))
    if page_size is None:
        rows = q.limit(MAX_EXPORT_ROWS).all()
    else:
        rows = q.offset((page - 1) * page_size).limit(page_size).all()

    blocks, subs, wards = _place_lookup(db, [r[0] for r in rows])
    cases = []
    for pw, n_anc, last, n_usg in rows:
        days = (today - last).days if last else None
        cases.append({
            "id": pw.id, "full_name": pw.full_name, "mobile_number": pw.mobile_number, "rch_id": pw.rch_id,
            "risk_factors": pw.risk_factors, "edd_date": pw.edd_date, "registered_on": pw.created_at,
            "last_anc_visit": last, "days_since_last_anc": days, "total_anc_visits": int(n_anc or 0),
            "usg_scans_completed": int(n_usg or 0),
            "follow_up_status": "Never seen" if last is None else ("Recent" if days <= fu_days else "Needs attention"),
            "block_name": blocks.get(pw.block_id), "sub_centre_name": subs.get(pw.sub_centre_id), "ward_name": wards.get(pw.ward_id),
            **_person_place(pw),
        })

    return {
        "report_period": {"start_date": start, "end_date": end, "date_basis": basis},
        "filters": {**f.describe(), **{k: v for k, v in p.items() if v not in (None, "", 0) and k not in ("sort_by", "sort_dir")}},
        "summary": {
            "total_high_risk_cases": total,
            "cases_with_recent_followup": int(n_recent or 0),
            "cases_needing_attention": int(n_attn or 0),
            "never_seen_for_anc": int(n_never or 0),
            "without_completed_usg": int(n_no_usg or 0),
            "followup_window_days": fu_days,
        },
        "risk_factors_breakdown": risk_factors_count,
        "breakdowns": {"by_block": by_block, "by_sub_centre": by_sub, "by_age_band": by_age, "by_trimester": by_tri},
        "cases": cases,
        "pagination": _pager(total, page, page_size or total or 1),
        "visualization": generate_visualization_config(
            [{"factor": k, "count": v} for k, v in risk_factors_count.items()], "bar", "factor", "count", "Risk Factors Distribution"),
    }


def _hr_params(date_basis, followup, followup_days, anc_visits_min, anc_visits_max, usg_done, edd_within_days, search, sort_by, sort_dir):
    return {
        "date_basis": date_basis if date_basis in ("registered", "edd", "any") else "registered",
        "followup": followup if followup in ("recent", "attention", "never") else None,
        "followup_days": followup_days, "anc_visits_min": anc_visits_min, "anc_visits_max": anc_visits_max,
        "usg_done": usg_done if usg_done in ("yes", "no") else None,
        "edd_within_days": edd_within_days, "search": (search or "").strip() or None,
        "sort_by": sort_by if sort_by in HR_SORTS else "registered", "sort_dir": "asc" if sort_dir == "asc" else "desc",
    }


@router.get("/high-risk-tracking")
async def get_high_risk_tracking_report(
    f: PWFilters = Depends(hr_filters_dep),
    date_basis: str = Query("registered", description="registered | edd | any"),
    followup: Optional[str] = Query(None, description="recent | attention | never"),
    followup_days: int = Query(30, ge=7, le=180),
    anc_visits_min: Optional[int] = Query(None, ge=0),
    anc_visits_max: Optional[int] = Query(None, ge=0),
    usg_done: Optional[str] = Query(None, description="yes | no – has a completed USG"),
    edd_within_days: Optional[int] = Query(None, ge=1, le=365),
    search: Optional[str] = None,
    sort_by: str = "registered",
    sort_dir: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Track high-risk pregnancies with follow-up status – fully filterable, sortable and paginated in SQL"""
    p = _hr_params(date_basis, followup, followup_days, anc_visits_min, anc_visits_max, usg_done, edd_within_days, search, sort_by, sort_dir)
    key = get_cache_key("high-risk-tracking", {"u": current_user.id, "f": f.describe(), "p": p, "page": page, "ps": page_size})
    cached = get_cached_report(key)
    if cached:
        return cached
    result = _run_high_risk_report(db, current_user, f, p, page, page_size)
    set_cached_report(key, result)
    return result


@router.get("/high-risk-tracking/export/{format}")
async def export_high_risk_tracking_report(
    format: str,
    f: PWFilters = Depends(hr_filters_dep),
    date_basis: str = Query("registered"),
    followup: Optional[str] = None,
    followup_days: int = Query(30, ge=7, le=180),
    anc_visits_min: Optional[int] = Query(None, ge=0),
    anc_visits_max: Optional[int] = Query(None, ge=0),
    usg_done: Optional[str] = None,
    edd_within_days: Optional[int] = Query(None, ge=1, le=365),
    search: Optional[str] = None,
    sort_by: str = "registered",
    sort_dir: str = "desc",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    if format not in ("excel", "csv"):
        raise HTTPException(status_code=400, detail="Format must be 'excel' or 'csv'")
    p = _hr_params(date_basis, followup, followup_days, anc_visits_min, anc_visits_max, usg_done, edd_within_days, search, sort_by, sort_dir)
    data = _run_high_risk_report(db, current_user, f, p, 1, None)
    cols = ["full_name", "mobile_number", "rch_id", "age", "blood_group", "gravida", "risk_factors", "gestational_weeks", "trimester",
            "edd_date", "registered_on", "last_anc_visit", "days_since_last_anc", "total_anc_visits", "usg_scans_completed",
            "follow_up_status", "block_name", "sub_centre_name", "ward_name"]
    rows = [{c: (r[c].date() if isinstance(r[c], datetime) else r[c]) for c in cols} for r in data["cases"]]
    stamp = date.today().isoformat()
    if format == "excel":
        summary = [{"metric": k.replace("_", " ").title(), "value": v} for k, v in data["summary"].items()]
        return await generate_excel_report({"High Risk Cases": rows, "Summary": summary}, f"high_risk_tracking_{stamp}.xlsx")
    return await generate_csv_report(rows, f"high_risk_tracking_{stamp}.csv")


# ── 2. ANC VISIT COMPLIANCE REPORT ───────────────────────────────────────────

ANC_SORTS = ("registered", "name", "age", "expected", "actual", "gap", "last_visit", "next_due")


def _expected_visits(weeks: Optional[int]) -> int:
    if weeks is None:
        return 1
    if weeks < 12:
        return 1
    if weeks < 28:
        return 2
    if weeks < 36:
        return 3
    return 4


def _anc_params(compliance, search, sort_by, sort_dir):
    return {
        "compliance": compliance if compliance in ("compliant", "non_compliant") else None,
        "search": (search or "").strip() or None,
        "sort_by": sort_by if sort_by in ANC_SORTS else "registered",
        "sort_dir": "asc" if sort_dir == "asc" else "desc",
    }


def _run_anc_compliance_report(db: Session, user: User, f: PWFilters, p: dict, page: int, page_size: Optional[int]):
    """
    ANC compliance for ongoing pregnancies.

    Cohort  : women registered inside the date range whose pregnancy is still ongoing
              (active, no recorded outcome, EDD not yet passed).
    Actual  : ALL ANC visits the woman has had so far.  (The date range selects the
              cohort; it must not also throw away visits made before the range - that
              made long-registered women look non-compliant whenever the range was short.)
    Expected: gestation-adjusted, see _expected_visits().
    """
    PW = PregnantWoman
    today = date.today()
    end = f.end_date or today
    start = f.start_date or (end - timedelta(days=90))
    f.case_status = "all"   # "ongoing" is defined explicitly below

    ongoing = and_(
        PW.is_active == True,  # noqa: E712
        or_(PW.pregnancy_outcome.is_(None), PW.pregnancy_outcome == ""),
        or_(
            PW.edd_date >= today,
            and_(PW.edd_date.is_(None), PW.lmp_date.isnot(None), PW.lmp_date >= today - timedelta(days=FULL_TERM_DAYS)),
        ),
    )
    conds = pw_conditions(f, user) + [ongoing, PW.created_at >= _day_start(start), PW.created_at < _day_end(end)]
    sc = _search_cond(p["search"], [PW.full_name, PW.mobile_number, PW.rch_id])
    if sc is not None:
        conds.append(sc)

    win = db.query(
        ANCVisit.pregnant_woman_id.label("pid"),
        func.count(ANCVisit.id).label("n"),
        func.max(ANCVisit.visit_date).label("last"),
        func.max(ANCVisit.next_visit_date).label("next_due"),
    ).group_by(ANCVisit.pregnant_woman_id).subquery()

    total = db.query(func.count(PW.id)).filter(*conds).scalar() or 0

    rows_all = db.query(PW.id, PW.lmp_date, PW.edd_date, win.c.n).outerjoin(win, win.c.pid == PW.id).filter(*conds).all()
    # Compliance depends on the *individual* expected count (varies with gestation), so it
    # is resolved in Python once per row rather than as one blanket SQL threshold.
    compliant_ids, non_compliant_ids = [], []
    for pid, lmp, edd, n in rows_all:
        expected = _expected_visits(gestational_weeks(lmp, edd, today))
        (compliant_ids if int(n or 0) >= expected else non_compliant_ids).append(pid)
    compliant, non_compliant = len(compliant_ids), len(non_compliant_ids)

    ids = compliant_ids if p["compliance"] == "compliant" else non_compliant_ids if p["compliance"] == "non_compliant" else None
    filtered_conds = list(conds)
    if ids is not None:
        filtered_conds.append(PW.id.in_(ids or [-1]))
        filtered_total = len(ids)
    else:
        filtered_total = total

    python_sort = p["sort_by"] in ("expected", "actual", "gap", "next_due")
    sort_map = {"registered": PW.created_at, "name": PW.full_name, "age": PW.age, "last_visit": win.c.last}
    order = [PW.created_at.desc(), PW.id.asc()] if python_sort else _order_by(sort_map, p["sort_by"], p["sort_dir"], "registered")

    q = db.query(PW, win.c.n, win.c.last, win.c.next_due).outerjoin(win, win.c.pid == PW.id).filter(*filtered_conds).order_by(*order)
    page_rows = q.all() if (python_sort or page_size is None) else q.offset((page - 1) * page_size).limit(page_size).all()

    blocks, subs, wards = _place_lookup(db, [r[0] for r in page_rows])
    compliance_data = []
    for pw, n, last, next_due in page_rows:
        weeks = gestational_weeks(pw.lmp_date, pw.edd_date, today)
        expected = _expected_visits(weeks)
        actual = int(n or 0)
        compliance_data.append({
            "id": pw.id, "full_name": pw.full_name, "mobile_number": pw.mobile_number,
            "expected_visits": expected, "actual_visits": actual, "gap": max(expected - actual, 0),
            "compliance_status": "Compliant" if actual >= expected else "Non-Compliant",
            "last_visit_date": last, "days_since_last_visit": (today - last).days if last else None,
            "next_visit_due": next_due,
            "block_name": blocks.get(pw.block_id), "sub_centre_name": subs.get(pw.sub_centre_id), "ward_name": wards.get(pw.ward_id),
            **_person_place(pw),
        })
    if python_sort:
        key = {"expected": "expected_visits", "actual": "actual_visits", "gap": "gap", "next_due": "next_visit_due"}[p["sort_by"]]
        compliance_data.sort(key=lambda r: (r[key] is None, r[key]), reverse=(p["sort_dir"] == "desc"))
        if page_size is not None:
            compliance_data = compliance_data[(page - 1) * page_size: page * page_size]

    def joined(qb):
        return qb.select_from(PW).outerjoin(win, win.c.pid == PW.id).filter(*conds)
    by_block = _group_counts(db, joined, PW.block_id, Block) if user.role == "district" and not f.block_id else []
    by_sub = _group_counts(db, joined, PW.sub_centre_id, SubCentre) if (user.role == "block" or f.block_id) and not f.sub_centre_id else []

    return {
        "report_period": {"start_date": start, "end_date": end, "basis": "registration date"},
        "filters": {**f.describe(), **{k: v for k, v in p.items() if v and k not in ("sort_by", "sort_dir")}},
        "summary": {
            "total_active_pregnancies": total, "compliant": compliant, "non_compliant": non_compliant,
            "compliance_rate": _pct2(compliant, total),
        },
        "breakdowns": {"by_block": by_block, "by_sub_centre": by_sub},
        "compliance_data": compliance_data,
        "pagination": _pager(filtered_total, page, page_size or filtered_total or 1),
        "visualization": generate_visualization_config(
            [{"status": "Compliant", "count": compliant}, {"status": "Non-Compliant", "count": non_compliant}],
            "pie", "status", "count", "ANC Compliance Status"),
    }


def _pct2(n, d):
    return round((n / d * 100) if d else 0, 2)


@router.get("/anc-compliance")
async def get_anc_compliance_report(
    f: PWFilters = Depends(anc_filters_dep),
    compliance: Optional[str] = Query(None, description="compliant | non_compliant"),
    search: Optional[str] = None,
    sort_by: str = "registered",
    sort_dir: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Track ANC visit compliance against gestation-adjusted expected visit counts"""
    p = _anc_params(compliance, search, sort_by, sort_dir)
    key = get_cache_key("anc-compliance", {"u": current_user.id, "f": f.describe(), "p": p, "page": page, "ps": page_size})
    cached = get_cached_report(key)
    if cached:
        return cached
    result = _run_anc_compliance_report(db, current_user, f, p, page, page_size)
    set_cached_report(key, result)
    return result


@router.get("/anc-compliance/export/{format}")
async def export_anc_compliance_report(
    format: str,
    f: PWFilters = Depends(anc_filters_dep),
    compliance: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: str = "registered",
    sort_dir: str = "desc",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    if format not in ("excel", "csv"):
        raise HTTPException(status_code=400, detail="Format must be 'excel' or 'csv'")
    p = _anc_params(compliance, search, sort_by, sort_dir)
    data = _run_anc_compliance_report(db, current_user, f, p, 1, None)
    cols = ["full_name", "mobile_number", "age", "gestational_weeks", "trimester", "expected_visits", "actual_visits", "gap",
            "compliance_status", "last_visit_date", "days_since_last_visit", "next_visit_due", "block_name", "sub_centre_name", "ward_name"]
    rows = [{c: r[c] for c in cols} for r in data["compliance_data"]]
    stamp = date.today().isoformat()
    if format == "excel":
        summary = [{"metric": k.replace("_", " ").title(), "value": v} for k, v in data["summary"].items()]
        return await generate_excel_report({"ANC Compliance": rows, "Summary": summary}, f"anc_compliance_{stamp}.xlsx")
    return await generate_csv_report(rows, f"anc_compliance_{stamp}.csv")


# ── 3. USG APPOINTMENT STATUS REPORT ─────────────────────────────────────────

USG_SORTS = ("scheduled", "name", "status", "days_pending")


def _usg_params(status_filter, appointment_type, scan_type, overdue_only, usg_centre_id, search, sort_by, sort_dir):
    status_filter = (status_filter or "").strip().lower() or None
    appointment_type = (appointment_type or "").strip().lower() or None
    if status_filter and status_filter not in {s.value for s in AppointmentStatus}:
        raise HTTPException(status_code=400, detail=f"Invalid status_filter '{status_filter}'")
    if appointment_type and appointment_type not in {t.value for t in AppointmentType}:
        raise HTTPException(status_code=400, detail=f"Invalid appointment_type '{appointment_type}'")
    return {
        "status": status_filter, "appointment_type": appointment_type,
        "scan_type": (scan_type or "").strip() or None, "overdue_only": bool(overdue_only), "usg_centre_id": usg_centre_id,
        "search": (search or "").strip() or None,
        "sort_by": sort_by if sort_by in USG_SORTS else "scheduled", "sort_dir": "asc" if sort_dir == "asc" else "desc",
    }


def _run_usg_status_report(db: Session, user: User, f: PWFilters, p: dict, page: int, page_size: Optional[int]):
    PW, U = PregnantWoman, USGAppointment
    today = date.today()
    end = f.end_date or today
    start = f.start_date or (end - timedelta(days=30))

    if user.role == "usg_centre":
        # A USG centre sees everything booked at ITS centre, wherever the woman lives.
        pw_conds = pw_conditions(f, user, include_scope=False)
        centre_scope = [U.usg_centre_id == user.usg_centre_id] if user.usg_centre_id else [false()]
    else:
        pw_conds = pw_conditions(f, user, include_scope=True)
        centre_scope = []

    # Everything except the status / overdue selectors: this set drives the summary cards and the
    # status chart, so they always describe the same population the filters describe.
    base_conds = list(pw_conds) + centre_scope + [U.scheduled_date >= _day_start(start), U.scheduled_date < _day_end(end)]
    if p["appointment_type"]:
        base_conds.append(U.appointment_type == AppointmentType(p["appointment_type"]))
    if p["scan_type"]:
        base_conds.append(U.scan_type == p["scan_type"])
    if p["usg_centre_id"]:
        base_conds.append(U.usg_centre_id == p["usg_centre_id"])
    sc = _search_cond(p["search"], [PW.full_name, PW.mobile_number])
    if sc is not None:
        base_conds.append(sc)

    conds = list(base_conds)
    if p["status"]:
        conds.append(U.status == AppointmentStatus(p["status"]))
    if p["overdue_only"]:
        conds += [U.status.in_(OPEN_USG_STATUSES), U.scheduled_date < _day_start(today)]

    def on(q, cs):
        return q.select_from(U).join(PW, PW.id == U.pregnant_woman_id).filter(*cs)

    total = on(db.query(func.count(U.id)), conds).scalar() or 0

    status_rows = on(db.query(U.status, func.count(U.id)), base_conds).group_by(U.status).all()
    status_breakdown = {_enum_key(k): int(v or 0) for k, v in status_rows}
    scoped_total = sum(status_breakdown.values())

    overdue_count = on(db.query(func.count(U.id)), base_conds + [U.status.in_(OPEN_USG_STATUSES), U.scheduled_date < _day_start(today)]).scalar() or 0

    centre_rows = _group_counts(db, lambda qb: on(qb, conds), U.usg_centre_id, USGCentre)
    type_rows = [{"type": _enum_key(k), "count": int(v or 0)} for k, v in
                 on(db.query(U.appointment_type, func.count(U.id)), conds).group_by(U.appointment_type).all()]

    sort_map = {"scheduled": U.scheduled_date, "name": PW.full_name, "status": U.status}
    if p["sort_by"] == "days_pending":
        # oldest open appointment first == most days pending; direction handled in SQL, no per-page re-sort
        open_first = case((U.status.in_(OPEN_USG_STATUSES), 0), else_=1)
        order = [open_first, U.scheduled_date.asc() if p["sort_dir"] == "desc" else U.scheduled_date.desc(), U.id.asc()]
    else:
        col = sort_map[p["sort_by"]]
        order = [col.desc() if p["sort_dir"] == "desc" else col.asc(), U.id.asc()]
    q = on(db.query(U, PW), conds).order_by(*order)
    rows = q.limit(MAX_EXPORT_ROWS).all() if page_size is None else q.offset((page - 1) * page_size).limit(page_size).all()

    centres = _name_map_local(db, USGCentre, [u.usg_centre_id for u, _ in rows])
    appointments = []
    for apt, pw in rows:
        is_open = _enum_key(apt.status) in ("scheduled", "accepted", "rescheduled")
        overdue = is_open and apt.scheduled_date.date() < today
        appointments.append({
            "id": apt.id, "pregnant_woman_id": pw.id, "pregnant_woman_name": pw.full_name, "mobile_number": pw.mobile_number,
            "usg_centre_name": centres.get(apt.usg_centre_id), "scheduled_date": apt.scheduled_date,
            "status": _enum_key(apt.status), "appointment_type": _enum_key(apt.appointment_type), "scan_type": apt.scan_type,
            "trimester": apt.trimester, "is_high_risk": bool(apt.is_high_risk),
            "days_pending": (today - apt.scheduled_date.date()).days if overdue else 0,
            "is_overdue": overdue, "completed_date": apt.completed_date, "reschedule_count": apt.reschedule_count,
        })

    return {
        "report_period": {"start_date": start, "end_date": end, "basis": "scheduled date"},
        "filters": {**f.describe(), **{k: v for k, v in p.items() if v not in (None, "", False) and k not in ("sort_by", "sort_dir")}},
        "summary": {
            "total_appointments": scoped_total,          # every status, all filters except the status selector
            "matching_filters": int(total),              # rows in the table (status selector applied as well)
            "status_breakdown": status_breakdown,
            "overdue_appointments": int(overdue_count),
            "completion_rate": _pct2(status_breakdown.get("completed", 0), scoped_total),
        },
        "breakdowns": {"by_centre": centre_rows, "by_type": type_rows},
        "appointments": appointments,
        "pagination": _pager(total, page, page_size or total or 1),
        "visualization": generate_visualization_config(
            [{"status": k, "count": v} for k, v in status_breakdown.items()], "bar", "status", "count", "USG Appointment Status Distribution"),
    }


def _name_map_local(db, model, ids):
    ids = [i for i in set(ids) if i is not None]
    if not ids:
        return {}
    return {r.id: r.name for r in db.query(model.id, model.name).filter(model.id.in_(ids)).all()}


@router.get("/usg-status")
async def get_usg_status_report(
    f: PWFilters = Depends(usg_filters_dep),
    status_filter: Optional[str] = None,
    appointment_type: Optional[str] = None,
    scan_type: Optional[str] = None,
    overdue_only: bool = False,
    usg_centre_id: Optional[int] = None,
    search: Optional[str] = None,
    sort_by: str = "scheduled",
    sort_dir: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Track USG appointment status, including pending, overdue and by-centre performance"""
    p = _usg_params(status_filter, appointment_type, scan_type, overdue_only, usg_centre_id, search, sort_by, sort_dir)
    key = get_cache_key("usg-status", {"u": current_user.id, "f": f.describe(), "p": p, "page": page, "ps": page_size})
    cached = get_cached_report(key)
    if cached:
        return cached
    result = _run_usg_status_report(db, current_user, f, p, page, page_size)
    set_cached_report(key, result)
    return result


@router.get("/usg-status/export/{format}")
async def export_usg_status_report(
    format: str,
    f: PWFilters = Depends(usg_filters_dep),
    status_filter: Optional[str] = None,
    appointment_type: Optional[str] = None,
    scan_type: Optional[str] = None,
    overdue_only: bool = False,
    usg_centre_id: Optional[int] = None,
    search: Optional[str] = None,
    sort_by: str = "scheduled",
    sort_dir: str = "desc",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    if format not in ("excel", "csv"):
        raise HTTPException(status_code=400, detail="Format must be 'excel' or 'csv'")
    p = _usg_params(status_filter, appointment_type, scan_type, overdue_only, usg_centre_id, search, sort_by, sort_dir)
    data = _run_usg_status_report(db, current_user, f, p, 1, None)
    cols = ["pregnant_woman_name", "mobile_number", "usg_centre_name", "scheduled_date", "status", "appointment_type",
            "scan_type", "trimester", "is_high_risk", "days_pending", "is_overdue", "completed_date"]
    rows = [{c: r[c] for c in cols} for r in data["appointments"]]
    stamp = date.today().isoformat()
    if format == "excel":
        summary = [{"metric": "Total Appointments", "value": data["summary"]["total_appointments"]},
                   {"metric": "Overdue", "value": data["summary"]["overdue_appointments"]},
                   {"metric": "Completion Rate (%)", "value": data["summary"]["completion_rate"]}]
        return await generate_excel_report({"USG Appointments": rows, "Summary": summary}, f"usg_status_{stamp}.xlsx")
    return await generate_csv_report(rows, f"usg_status_{stamp}.csv")



# 4. GRIEVANCE RESOLUTION PERFORMANCE REPORT
@router.get("/grievance-performance")
async def get_grievance_performance_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Analyze grievance resolution performance"""
    if current_user.role not in ("district", "block"):
        raise HTTPException(status_code=403, detail="District or Block user access required")
    cache_key = get_cache_key("grievance-performance", {
        "user_id": current_user.id,
        "start_date": start_date,
        "end_date": end_date
    })
    cached = get_cached_report(cache_key)
    if cached:
        return cached
    
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=30)
    
    # Base query
    query = db.query(Grievance).filter(
        _dt_between(Grievance.created_at, start_date, end_date)
    )
    
    if current_user.role == "district":
        query = query.filter(Grievance.district_id == current_user.district_id)
    elif current_user.role == "block":
        query = query.filter(Grievance.assigned_to_block == current_user.block_id)
    
    total_grievances = query.count()
    resolved_grievances = query.filter(Grievance.status == "resolved").all()
    escalated_grievances = query.filter(Grievance.escalated_to_district == True).count()
    
    # Calculate average resolution time
    resolution_times = []
    for grievance in resolved_grievances:
        if grievance.resolved_at:
            resolution_time = (grievance.resolved_at - grievance.created_at).days
            resolution_times.append(resolution_time)
    
    avg_resolution_time = sum(resolution_times) / len(resolution_times) if resolution_times else 0
    
    # Status breakdown
    status_breakdown = db.query(
        Grievance.status,
        func.count(Grievance.id).label('count')
    ).filter(_dt_between(Grievance.created_at, start_date, end_date))
    
    if current_user.role == "district":
        status_breakdown = status_breakdown.filter(Grievance.district_id == current_user.district_id)
    elif current_user.role == "block":
        status_breakdown = status_breakdown.filter(Grievance.assigned_to_block == current_user.block_id)
    
    status_breakdown = status_breakdown.group_by(Grievance.status).all()
    
    result = {
        "report_period": {
            "start_date": start_date,
            "end_date": end_date
        },
        "summary": {
            "total_grievances": total_grievances,
            "resolved_grievances": len(resolved_grievances),
            "pending_grievances": total_grievances - len(resolved_grievances),
            "escalated_grievances": escalated_grievances,
            "resolution_rate": round((len(resolved_grievances) / total_grievances * 100) if total_grievances > 0 else 0, 2),
            "average_resolution_time_days": round(avg_resolution_time, 1)
        },
        "status_breakdown": {status: count for status, count in status_breakdown},
        "resolution_time_distribution": {
            "0-3_days": sum(1 for t in resolution_times if t <= 3),
            "4-7_days": sum(1 for t in resolution_times if 3 < t <= 7),
            "8-14_days": sum(1 for t in resolution_times if 7 < t <= 14),
            "15+_days": sum(1 for t in resolution_times if t > 14)
        },
        "visualization": generate_visualization_config(
            [{"status": status, "count": count} for status, count in status_breakdown],
            "pie",
            "status",
            "count",
            "Grievance Status Distribution"
        )
    }
    
    set_cached_report(cache_key, result)
    return result

# SUB-CENTRE USER REPORTS
@router.get("/sub-centre/activity")
async def get_sub_centre_activity_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get sub-centre activity report"""
    if current_user.role != "sub_centre":
        raise HTTPException(status_code=403, detail="Sub-centre user access required")
    
    # Set default date range
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=30)
    
    # Get pregnant women under care
    pregnant_women = db.query(PregnantWoman).filter(
        sub_centre_match(current_user.sub_centre_id),
        PregnantWoman.is_active == True
    ).count()
    
    # Get ANC visits conducted
    anc_visits = db.query(ANCVisit).join(PregnantWoman)\
        .filter(sub_centre_match(current_user.sub_centre_id))\
        .filter(ANCVisit.visit_date.between(start_date, end_date)).count()
    
    # Get USG referrals made
    usg_referrals = db.query(ANCVisit).join(PregnantWoman)\
        .filter(sub_centre_match(current_user.sub_centre_id))\
        .filter(ANCVisit.referred_for_usg == True)\
        .filter(ANCVisit.visit_date.between(start_date, end_date)).count()
    
    return {
        "report_period": {
            "start_date": start_date,
            "end_date": end_date
        },
        "activity_summary": {
            "pregnant_women_under_care": pregnant_women,
            "anc_visits_conducted": anc_visits,
            "usg_referrals_made": usg_referrals,
            "average_visits_per_day": round(anc_visits / ((end_date - start_date).days + 1), 2)
        }
    }

# USG CENTRE USER REPORTS
@router.get("/usg-centre/appointments")
async def get_usg_centre_appointment_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get USG centre appointment report"""
    if current_user.role != "usg_centre":
        raise HTTPException(status_code=403, detail="USG centre user access required")
    
    # Set default date range
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=30)
    
    # Get appointment statistics
    total_appointments = db.query(USGAppointment).filter(
        USGAppointment.usg_centre_id == current_user.usg_centre_id,
        _dt_between(USGAppointment.scheduled_date, start_date, end_date)
    ).count()
    
    completed_appointments = db.query(USGAppointment).filter(
        USGAppointment.usg_centre_id == current_user.usg_centre_id,
        USGAppointment.status == "completed",
        _dt_between(USGAppointment.scheduled_date, start_date, end_date)
    ).count()
    
    emergency_appointments = db.query(USGAppointment).filter(
        USGAppointment.usg_centre_id == current_user.usg_centre_id,
        USGAppointment.appointment_type == "emergency",
        _dt_between(USGAppointment.scheduled_date, start_date, end_date)
    ).count()
    
    return {
        "report_period": {
            "start_date": start_date,
            "end_date": end_date
        },
        "appointment_summary": {
            "total_appointments": total_appointments,
            "completed_appointments": completed_appointments,
            "emergency_appointments": emergency_appointments,
            "completion_rate": round((completed_appointments / total_appointments * 100) if total_appointments > 0 else 0, 2),
            "average_appointments_per_day": round(total_appointments / ((end_date - start_date).days + 1), 2)
        }
    }


# ============================================
# PHASE 2: ADVANCED ANALYTICS
# ============================================

# 5. COMPARATIVE PERFORMANCE ANALYSIS
compare_filters_dep = make_pw_filters_dep("all")


@router.get("/comparative-analysis")
async def get_comparative_analysis(
    f: PWFilters = Depends(compare_filters_dep),
    comparison_type: str = "block",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Compare performance across blocks (district users) or sub-centres (block users).

    Beneficiary filters (risk, age band, trimester, ward, ...) narrow the cohort of women
    behind every column.  `total_pregnant_women` / `high_risk_cases` are stock counts of
    women with an active pregnancy; ANC / USG / PMSMA columns are activity inside the
    date range (and count visits of women who have since delivered).
    """
    if current_user.role not in ("district", "block"):
        raise HTTPException(status_code=403, detail="District or Block user access required")
    if comparison_type == "sub_centre" and current_user.role == "district":
        raise HTTPException(status_code=400, detail="District users can only use comparison_type=block. Use block user login for sub_centre comparison.")
    if comparison_type == "block" and current_user.role == "block":
        raise HTTPException(status_code=400, detail="Block users can only use comparison_type=sub_centre.")
    if comparison_type not in ("block", "sub_centre"):
        raise HTTPException(status_code=400, detail="comparison_type must be 'block' or 'sub_centre'")

    end_date = f.end_date or date.today()
    start_date = f.start_date or (end_date - timedelta(days=30))
    PW = PregnantWoman

    cohort_all = pw_conditions(f, current_user)                       # scope + every beneficiary filter
    cohort_active = cohort_all + [PW.is_active == True]                # noqa: E712

    if current_user.role == "district":
        group_col = PW.block_id
        ent_q = db.query(Block.id, Block.name).filter(Block.district_id == current_user.district_id, Block.is_active == True)  # noqa: E712
        if f.block_id:
            ent_q = ent_q.filter(Block.id == f.block_id)
        entities = ent_q.order_by(Block.name).all()
    else:
        group_col = PW.sub_centre_id
        ent_q = db.query(SubCentre.id, SubCentre.name).filter(SubCentre.block_id == current_user.block_id, SubCentre.is_active == True)  # noqa: E712
        if f.sub_centre_id:
            ent_q = ent_q.filter(SubCentre.id == f.sub_centre_id)
        entities = ent_q.order_by(SubCentre.name).all()

    def grouped(q):
        return {k: int(v or 0) for k, v in q.group_by(group_col).all()}

    total_pw = grouped(db.query(group_col, func.count(PW.id)).filter(*cohort_active))
    high_risk = grouped(db.query(group_col, func.count(PW.id)).filter(*cohort_active, PW.is_high_risk == True))  # noqa: E712
    anc = grouped(db.query(group_col, func.count(ANCVisit.id)).select_from(ANCVisit).join(PW, PW.id == ANCVisit.pregnant_woman_id)
                  .filter(*cohort_all, ANCVisit.visit_date >= start_date, ANCVisit.visit_date <= end_date))
    anc_ref = grouped(db.query(group_col, func.count(ANCVisit.id)).select_from(ANCVisit).join(PW, PW.id == ANCVisit.pregnant_woman_id)
                      .filter(*cohort_all, ANCVisit.referred_for_usg == True, ANCVisit.visit_date >= start_date, ANCVisit.visit_date <= end_date))  # noqa: E712
    usg = grouped(db.query(group_col, func.count(USGAppointment.id)).select_from(USGAppointment).join(PW, PW.id == USGAppointment.pregnant_woman_id)
                  .filter(*cohort_all, USGAppointment.status == AppointmentStatus.COMPLETED, _dt_between(USGAppointment.scheduled_date, start_date, end_date)))
    pmsma = grouped(db.query(group_col, func.count(PMSMASession.id)).select_from(PMSMASession).join(PW, PW.id == PMSMASession.pregnant_woman_id)
                    .filter(*cohort_all, PMSMASession.status == "completed", _dt_between(PMSMASession.scheduled_date, start_date, end_date)))
    griev = {}
    if current_user.role == "district":
        griev = {k: int(v or 0) for k, v in db.query(Grievance.assigned_to_block, func.count(Grievance.id)).filter(
            Grievance.district_id == current_user.district_id, Grievance.status == "resolved",
            _dt_between(Grievance.created_at, start_date, end_date)).group_by(Grievance.assigned_to_block).all()}

    comparative_data = []
    for ent_id, ent_name in entities:
        tw, an, ug = total_pw.get(ent_id, 0), anc.get(ent_id, 0), usg.get(ent_id, 0)
        row = {
            "id": ent_id, "name": ent_name,
            "total_pregnant_women": tw, "high_risk_cases": high_risk.get(ent_id, 0),
            "anc_visits": an, "usg_completed": ug, "pmsma_completed": pmsma.get(ent_id, 0),
            "performance_score": round((an / tw if tw > 0 else 0) * 50 + (ug / tw if tw > 0 else 0) * 50, 2),
        }
        if current_user.role == "district":
            row["grievances_resolved"] = griev.get(ent_id, 0)
        else:
            row["usg_referrals"] = anc_ref.get(ent_id, 0)
        comparative_data.append(row)

    comparative_data.sort(key=lambda x: x["performance_score"], reverse=True)

    return {
        "report_period": {"start_date": start_date, "end_date": end_date},
        "filters": f.describe(),
        "comparison_type": comparison_type,
        "total_entities": len(comparative_data),
        "data": comparative_data,
        "best_performer": comparative_data[0] if comparative_data else None,
        "worst_performer": comparative_data[-1] if len(comparative_data) > 1 else None,
        "visualization": generate_visualization_config(comparative_data, "bar", "name", "performance_score", "Comparative Performance")
    }


# 6. TREND ANALYSIS
trend_filters_dep = make_pw_filters_dep("all")
TREND_METRICS = ("registrations", "anc_visits", "usg_appointments", "grievances", "pmsma_sessions", "deliveries", "mobilisation_cases")


def _trend_month_starts(months: int) -> List[date]:
    """First day of each of the last `months` calendar months, oldest first, ending with the current month."""
    y, m = date.today().year, date.today().month
    out = []
    for _ in range(months):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


@router.get("/trends")
async def get_trend_analysis(
    f: PWFilters = Depends(trend_filters_dep),
    months: int = Query(6, ge=1, le=24, description="How many calendar months to show, ending with the current one"),
    metric: str = "registrations",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Month-by-month counts for the caller's jurisdiction.  Beneficiary filters (block, sub-centre, ward,
    risk, age band, ...) apply to every metric that is about women; the date filters are ignored
    because `months` decides the window.
    """
    if metric not in TREND_METRICS:
        raise HTTPException(status_code=400, detail=f"metric must be one of {', '.join(TREND_METRICS)}")
    role = current_user.role
    allowed = {
        "registrations": ("district", "block", "sub_centre"),
        "anc_visits": ("district", "block", "sub_centre"),
        "usg_appointments": ("district", "block", "sub_centre", "usg_centre"),
        "grievances": ("district", "block"),
        "pmsma_sessions": ("district", "block", "sub_centre", "pmsma"),
        "deliveries": ("district", "block", "sub_centre", "dp", "pmsma"),
        "mobilisation_cases": ("district", "block", "sub_centre"),
    }
    if role not in allowed[metric]:
        raise HTTPException(status_code=403, detail="Not authorised for this trend metric")

    PW = PregnantWoman
    cohort_scoped = pw_conditions(f, current_user, include_scope=role not in ("usg_centre", "pmsma", "dp"))
    cohort_only = pw_conditions(f, None, include_scope=False)

    def count_for(m_start: date, m_end: date) -> int:
        if metric == "registrations":
            q = db.query(func.count(PW.id)).filter(*cohort_scoped, _dt_between(PW.created_at, m_start, m_end))
        elif metric == "anc_visits":
            q = db.query(func.count(ANCVisit.id)).select_from(ANCVisit).join(PW, PW.id == ANCVisit.pregnant_woman_id) \
                .filter(*cohort_scoped, ANCVisit.visit_date >= m_start, ANCVisit.visit_date <= m_end)
        elif metric == "usg_appointments":
            q = db.query(func.count(USGAppointment.id)).select_from(USGAppointment).join(PW, PW.id == USGAppointment.pregnant_woman_id) \
                .filter(*cohort_scoped, _dt_between(USGAppointment.scheduled_date, m_start, m_end))
            if role == "usg_centre":
                q = q.filter(USGAppointment.usg_centre_id == current_user.usg_centre_id) if current_user.usg_centre_id else q.filter(false())
        elif metric == "grievances":
            q = db.query(func.count(Grievance.id)).filter(_dt_between(Grievance.created_at, m_start, m_end))
            q = q.filter(Grievance.district_id == current_user.district_id) if role == "district" else q.filter(Grievance.assigned_to_block == current_user.block_id)
        elif metric == "pmsma_sessions":
            q = db.query(func.count(PMSMASession.id)).select_from(PMSMASession).join(PW, PW.id == PMSMASession.pregnant_woman_id) \
                .filter(*cohort_only, _dt_between(PMSMASession.scheduled_date, m_start, m_end))
            if role == "pmsma":
                q = q.filter(PMSMASession.block_id == current_user.block_id) if current_user.block_id else q.filter(false())
            else:
                q = q.filter(*scope_conditions(current_user))
        elif metric == "deliveries":
            q = db.query(func.count(DeliveryOutcome.id))
            q = _build_delivery_role_filter(q, current_user, referral_model=False)
            q = _delivery_extra_filters(q, DeliveryOutcome, f, None).filter(_dt_between(DeliveryOutcome.delivery_date, m_start, m_end))
        else:  # mobilisation_cases
            q = db.query(func.count(MobilisationCase.id)).select_from(MobilisationCase).join(PW, PW.id == MobilisationCase.pregnant_woman_id) \
                .filter(*cohort_only, _dt_between(MobilisationCase.created_at, m_start, m_end))
            if role == "district":
                q = q.filter(MobilisationCase.district_id == current_user.district_id)
            elif role == "block":
                q = q.filter(MobilisationCase.block_id == current_user.block_id)
            else:
                q = q.filter(MobilisationCase.sub_centre_id == current_user.sub_centre_id)
        return int(q.scalar() or 0)

    starts = _trend_month_starts(months)
    trend_data = []
    for i, m_start in enumerate(starts):
        next_start = starts[i + 1] if i + 1 < len(starts) else (date(m_start.year + 1, 1, 1) if m_start.month == 12 else date(m_start.year, m_start.month + 1, 1))
        trend_data.append({"month": m_start.strftime("%Y-%m"), "count": count_for(m_start, next_start - timedelta(days=1))})

    total = sum(d["count"] for d in trend_data)
    return {
        "metric": metric,
        "months": months,
        "filters": {k: v for k, v in f.describe().items() if k not in ("start_date", "end_date")},
        "trend_data": trend_data,
        "summary": {"total": total, "average_per_month": round(total / len(trend_data), 2) if trend_data else 0},
        "visualization": generate_visualization_config(trend_data, "line", "month", "count", f"{metric.replace('_', ' ').title()} Trend")
    }

# ============================================
# PHASE 3: OPERATIONAL REPORTS
# ============================================

# 8. USER ACTIVITY REPORT
@router.get("/user-activity")
async def get_user_activity_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Track user activity and productivity"""
    if current_user.role not in ["district", "block"]:
        raise HTTPException(status_code=403, detail="District or Block user access required")
    
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=30)
    
    # Get users in jurisdiction
    query = db.query(User).filter(User.is_active == True)
    if current_user.role == "district":
        query = query.filter(User.district_id == current_user.district_id)
    elif current_user.role == "block":
        query = query.filter(User.block_id == current_user.block_id)
    
    users = query.all()
    user_activity = []
    
    for user in users:
        # Count registrations
        registrations = db.query(PregnantWoman).filter(PregnantWoman.registered_by == user.id, _dt_between(PregnantWoman.created_at, start_date, end_date)).count()
        # Count ANC visits
        anc_visits = db.query(ANCVisit).filter(ANCVisit.attended_by == user.id, ANCVisit.visit_date.between(start_date, end_date)).count()
        
        user_activity.append({
            "user_name": user.full_name,
            "role": user.role,
            "registrations": registrations,
            "anc_visits": anc_visits,
            "total_actions": registrations + anc_visits
        })
    
    user_activity.sort(key=lambda x: x['total_actions'], reverse=True)
    
    return {
        "report_period": {"start_date": start_date, "end_date": end_date},
        "user_activity": user_activity,
        "summary": {"total_users": len(users), "active_users": sum(1 for u in user_activity if u['total_actions'] > 0)},
        "visualization": generate_visualization_config(user_activity[:10], "bar", "user_name", "total_actions", "Top 10 Active Users")
    }

# 9. SMS DELIVERY REPORT
@router.get("/sms-delivery")
async def get_sms_delivery_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Track SMS delivery performance"""
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=30)
    
    # Get SMS logs
    sms_logs = db.query(SMSLog).filter(_dt_between(SMSLog.sent_at, start_date, end_date)).all()
    
    total_sms = len(sms_logs)
    delivered = sum(1 for log in sms_logs if log.delivery_status == "delivered")
    failed = sum(1 for log in sms_logs if log.delivery_status == "failed")
    
    # Message type breakdown
    type_breakdown = {}
    for log in sms_logs:
        type_breakdown[log.message_type] = type_breakdown.get(log.message_type, 0) + 1
    
    return {
        "report_period": {"start_date": start_date, "end_date": end_date},
        "summary": {
            "total_sms": total_sms,
            "delivered": delivered,
            "failed": failed,
            "delivery_rate": round((delivered / total_sms * 100) if total_sms > 0 else 0, 2)
        },
        "type_breakdown": type_breakdown,
        "visualization": generate_visualization_config([{"type": k, "count": v} for k, v in type_breakdown.items()], "pie", "type", "count", "SMS by Type")
    }

# ============================================
# DELIVERY REPORTS (Step 4)
# ============================================

delivery_filters_dep = make_pw_filters_dep("all")


def _delivery_dates(f: PWFilters):
    end = f.end_date or date.today()
    start = f.start_date or (end - timedelta(days=30))
    return start, end


@router.get("/delivery/summary")
async def get_delivery_summary_report(
    f: PWFilters = Depends(delivery_filters_dep),
    dp_id: Optional[int] = Query(None, description="Only referrals / outcomes at this delivery point"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Delivery referral and outcome summary for the current user's scope, filterable by geography / risk / delivery point."""
    start_date, end_date = _delivery_dates(f)
    d = get_delivery_summary_data(db, current_user, start_date, end_date, f=f, dp_id=dp_id)

    return {
        "report_period": {"start_date": start_date, "end_date": end_date},
        "filters": {**f.describe(), **({"dp_id": dp_id} if dp_id else {})},
        "referral_summary": {
            "total_referrals": d["total_referrals"],
            "pending": d["pending"],
            "accepted": d["accepted"],
            "re_referred": d["re_referred"],
            "completed": d["completed"],
        },
        "outcome_summary": {
            "total_outcomes": d["total_outcomes"],
            "by_type": d["outcomes"],
            "adverse_outcome_rate": d["adverse_rate"],
        },
        "visualization": generate_visualization_config(
            [{"type": k, "count": v} for k, v in d["outcomes"].items()],
            "donut", "type", "count", "Delivery Outcomes"
        ),
    }


@router.get("/delivery/outcome-breakdown")
async def get_delivery_outcome_breakdown(
    f: PWFilters = Depends(delivery_filters_dep),
    dp_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Delivery outcome breakdown with percentages, adverse summary and maternal-outcome mix."""
    start_date, end_date = _delivery_dates(f)

    def outcome_q(*cols):
        q = db.query(*cols)
        q = _build_delivery_role_filter(q, current_user, referral_model=False)
        q = _delivery_extra_filters(q, DeliveryOutcome, f, dp_id)
        return q.filter(_dt_between(DeliveryOutcome.delivery_date, start_date, end_date))

    rows = outcome_q(DeliveryOutcome.delivery_type, func.count(DeliveryOutcome.id)).group_by(DeliveryOutcome.delivery_type).all()
    outcome_dict: Dict[str, int] = {}
    for dt, c in rows:
        outcome_dict[_enum_key(dt)] = int(c or 0)
    total = sum(outcome_dict.values())

    breakdown = [
        {"delivery_type": dt, "count": cnt, "percentage": round((cnt / total * 100) if total > 0 else 0, 2)}
        for dt, cnt in outcome_dict.items()
    ]
    adverse_total = sum(outcome_dict.get(t, 0) for t in ("still_birth", "infant_death", "maternal_death"))

    mat_rows = outcome_q(DeliveryOutcome.maternal_outcome, func.count(DeliveryOutcome.id)).group_by(DeliveryOutcome.maternal_outcome).all()
    maternal = {_enum_key(k): int(v or 0) for k, v in mat_rows}

    return {
        "report_period": {"start_date": start_date, "end_date": end_date},
        "filters": {**f.describe(), **({"dp_id": dp_id} if dp_id else {})},
        "total_outcomes": total,
        "outcome_breakdown": breakdown,
        "adverse_summary": {
            "still_birth": outcome_dict.get("still_birth", 0),
            "infant_death": outcome_dict.get("infant_death", 0),
            "maternal_death": outcome_dict.get("maternal_death", 0),
            "total_adverse": adverse_total,
            "adverse_rate": round((adverse_total / total * 100) if total > 0 else 0, 2),
        },
        "maternal_outcome_breakdown": maternal,
        "visualization": generate_visualization_config(breakdown, "pie", "delivery_type", "count", "Outcome Breakdown"),
    }


@router.get("/delivery/point-performance")
async def get_delivery_point_performance(
    f: PWFilters = Depends(delivery_filters_dep),
    dp_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Per-delivery-point metrics for referrals raised by women in the caller's
    jurisdiction.  District and block roles only.
    """
    if current_user.role not in ("district", "block"):
        raise HTTPException(status_code=403, detail="District or Block user access required")

    start_date, end_date = _delivery_dates(f)
    district_id = _user_district_id(db, current_user)
    dps = []
    if district_id is not None:
        dp_q = db.query(DeliveryPoint).filter(DeliveryPoint.is_active == True, DeliveryPoint.district_id == district_id)  # noqa: E712
        if dp_id:
            dp_q = dp_q.filter(DeliveryPoint.id == dp_id)
        dps = dp_q.order_by(DeliveryPoint.name).all()
    dp_ids = [d.id for d in dps]

    ref_by_dp: Dict[int, dict] = {}
    out_by_dp: Dict[int, dict] = {}
    if dp_ids:
        ref_q = db.query(DeliveryReferral.dp_id, DeliveryReferral.status, func.count(DeliveryReferral.id))
        ref_q = _build_delivery_role_filter(ref_q, current_user, referral_model=True)
        ref_q = _delivery_extra_filters(ref_q, DeliveryReferral, f, None)
        ref_q = ref_q.filter(_dt_between(DeliveryReferral.created_at, start_date, end_date), DeliveryReferral.dp_id.in_(dp_ids))
        for r_dp, r_status, cnt in ref_q.group_by(DeliveryReferral.dp_id, DeliveryReferral.status).all():
            slot = ref_by_dp.setdefault(r_dp, {"pending": 0, "accepted": 0, "re_referred": 0, "completed": 0})
            slot[_enum_key(r_status)] = int(cnt or 0)

        # Acceptance time is averaged in Python (no MySQL-only TIMESTAMPDIFF) across every
        # referral that has been accepted, whatever its status has moved on to since.
        acc_q = db.query(DeliveryReferral.dp_id, DeliveryReferral.created_at, DeliveryReferral.accepted_at)
        acc_q = _build_delivery_role_filter(acc_q, current_user, referral_model=True)
        acc_q = _delivery_extra_filters(acc_q, DeliveryReferral, f, None)
        acc_q = acc_q.filter(
            _dt_between(DeliveryReferral.created_at, start_date, end_date),
            DeliveryReferral.dp_id.in_(dp_ids),
            DeliveryReferral.accepted_at.isnot(None),
        )
        hours: Dict[int, list] = {}
        for r_dp, created, accepted_at in acc_q.all():
            if created and accepted_at:
                hours.setdefault(r_dp, []).append(max((accepted_at - created).total_seconds(), 0) / 3600.0)
        for r_dp, vals in hours.items():
            ref_by_dp.setdefault(r_dp, {"pending": 0, "accepted": 0, "re_referred": 0, "completed": 0})["avg_accept_hours"] = round(sum(vals) / len(vals), 1)

        out_q = db.query(DeliveryOutcome.dp_id, DeliveryOutcome.delivery_type, func.count(DeliveryOutcome.id))
        out_q = _build_delivery_role_filter(out_q, current_user, referral_model=False)
        out_q = _delivery_extra_filters(out_q, DeliveryOutcome, f, None)
        out_q = out_q.filter(_dt_between(DeliveryOutcome.delivery_date, start_date, end_date), DeliveryOutcome.dp_id.in_(dp_ids))
        for o_dp, o_type, cnt in out_q.group_by(DeliveryOutcome.dp_id, DeliveryOutcome.delivery_type).all():
            out_by_dp.setdefault(o_dp, {})[_enum_key(o_type)] = int(cnt or 0)

    filtered = has_any_filter(f)
    dp_performance = []
    for dp in dps:
        ref = ref_by_dp.get(dp.id, {})
        out = out_by_dp.get(dp.id, {})
        total_received = sum(ref.get(k, 0) for k in ("pending", "accepted", "re_referred", "completed"))
        total_outcomes = sum(out.values())
        # Keep DPs that belong to the caller's own area even with no activity; hide unrelated empty ones.
        own_area = current_user.role == "district" or dp.block_id == current_user.block_id
        if not (total_received or total_outcomes or (own_area and not filtered)):
            continue
        accepted = ref.get("accepted", 0) + ref.get("completed", 0)
        re_referred = ref.get("re_referred", 0)
        dp_performance.append({
            "dp_id": dp.id,
            "dp_name": dp.name,
            "total_referrals_received": total_received,
            "accepted": accepted,
            "re_referred": re_referred,
            "completed": ref.get("completed", 0),
            "acceptance_rate": round((accepted / total_received * 100) if total_received > 0 else 0, 2),
            "re_referral_rate": round((re_referred / total_received * 100) if total_received > 0 else 0, 2),
            "avg_acceptance_time_hours": ref.get("avg_accept_hours", 0.0),
            "outcomes": {k: out.get(k, 0) for k in ("safe_delivery", "live_birth", "still_birth", "infant_death", "maternal_death")},
        })

    dp_performance.sort(key=lambda x: (x["completed"], x["total_referrals_received"]), reverse=True)

    return {
        "report_period": {"start_date": start_date, "end_date": end_date},
        "filters": {**f.describe(), **({"dp_id": dp_id} if dp_id else {})},
        "dp_performance": dp_performance,
        "visualization": generate_visualization_config(dp_performance, "bar", "dp_name", "completed", "Delivery Point Performance"),
    }


@router.get("/ecg-summary")
async def get_ecg_summary_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """ECG report summary — accessible to district, block, sub_centre, dp, pmsma roles."""
    if current_user.role not in ("district", "block", "sub_centre", "dp", "pmsma"):
        raise HTTPException(status_code=403, detail="Not authorized")

    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=30)

    query = db.query(ECGReport).filter(
        ECGReport.ecg_date >= start_date,
        ECGReport.ecg_date <= end_date
    )

    if current_user.role == "dp":
        query = query.filter(ECGReport.dp_id == current_user.dp_id)
    elif current_user.role == "sub_centre":
        pw_ids = db.query(PregnantWoman.id).filter(
            PregnantWoman.sub_centre_id == current_user.sub_centre_id
        ).subquery()
        query = query.filter(ECGReport.pregnant_woman_id.in_(pw_ids))
    elif current_user.role == "block":
        pw_ids = db.query(PregnantWoman.id).filter(
            PregnantWoman.block_id == current_user.block_id
        ).subquery()
        query = query.filter(ECGReport.pregnant_woman_id.in_(pw_ids))
    elif current_user.role == "district":
        # Match via pregnant woman's district_id OR via the DP's district_id
        pw_ids = db.query(PregnantWoman.id).filter(
            PregnantWoman.district_id == current_user.district_id
        ).subquery()
        dp_ids = db.query(DeliveryPoint.id).filter(
            DeliveryPoint.district_id == current_user.district_id
        ).subquery()
        query = query.filter(
            (ECGReport.pregnant_woman_id.in_(pw_ids)) |
            (ECGReport.dp_id.in_(dp_ids))
        )
    elif current_user.role == "pmsma":
        # PMSMA sees ECGs only for pregnant women it has referred itself
        own_ref_pw_ids = db.query(DeliveryReferral.pregnant_woman_id).filter(
            DeliveryReferral.referred_by_user_id == current_user.id
        ).distinct().subquery()
        query = query.filter(ECGReport.pregnant_woman_id.in_(own_ref_pw_ids))

    total = query.count()
    normal = query.filter(ECGReport.result == "normal").count()
    abnormal = query.filter(ECGReport.result == "abnormal").count()

    # Per-DP breakdown (useful for district/block)
    dp_breakdown = []
    if current_user.role in ("district", "block"):
        dp_rows = db.query(
            ECGReport.dp_id,
            func.count(ECGReport.id).label("total"),
            func.sum(case((ECGReport.result == "normal", 1), else_=0)).label("normal"),
            func.sum(case((ECGReport.result == "abnormal", 1), else_=0)).label("abnormal")
        ).filter(
            ECGReport.ecg_date >= start_date,
            ECGReport.ecg_date <= end_date
        )
        if current_user.role == "block":
            dp_rows = dp_rows.join(PregnantWoman, ECGReport.pregnant_woman_id == PregnantWoman.id).filter(
                PregnantWoman.block_id == current_user.block_id
            )
        elif current_user.role == "district":
            dp_ids_district = db.query(DeliveryPoint.id).filter(
                DeliveryPoint.district_id == current_user.district_id
            ).subquery()
            dp_rows = dp_rows.filter(ECGReport.dp_id.in_(dp_ids_district))
        dp_rows = dp_rows.group_by(ECGReport.dp_id).all()
        for row in dp_rows:
            dp = db.query(DeliveryPoint).filter(DeliveryPoint.id == row.dp_id).first()
            dp_breakdown.append({
                "dp_id": row.dp_id,
                "dp_name": dp.name if dp else None,
                "total": row.total,
                "normal": row.normal,
                "abnormal": row.abnormal,
            })

    return {
        "report_period": {"start_date": start_date, "end_date": end_date},
        "summary": {
            "total_ecg_reports": total,
            "normal": normal,
            "abnormal": abnormal,
            "abnormal_rate": round((abnormal / total * 100) if total > 0 else 0, 2),
        },
        "dp_breakdown": dp_breakdown,
    }


# CLEAR CACHE ENDPOINT
@router.post("/clear-cache")
async def clear_report_cache(current_user: User = Depends(get_current_active_user)):
    """Clear report cache (admin only)"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="District user access required")
    
    report_cache.clear()
    return {"message": "Report cache cleared successfully"}

