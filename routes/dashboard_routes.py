from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_, extract, case, false
from typing import List, Optional
from datetime import datetime, date

from database import get_db
from models import (
    User, PregnantWoman, USGAppointment,
    Block, SubCentre, USGCentre, ANCVisit,
    DeliveryReferral, DeliveryOutcome, DeliveryOutcomeBaby, DeliveryPoint, Grievance,
    MaternalOutcome, DeliveryType, UserWardMapping, WardSubcentreMapping, PMSMASession
)
from schemas import DashboardStats, BlockDashboardStats, DistrictDashboardStats
from auth import get_current_active_user

# ─────────────────────────────────────────────────────────────────────────────
# Role scoping helpers
#
# Every dashboard number must be limited to what the logged-in user can see in
# their own modules. These helpers mirror the scoping already used by:
#   • GET /pregnant-women/            (Beneficiaries list)
#   • GET /usg-appointments/          (USG Appointment list)
#   • GET /delivery-referrals/        (Delivery Referral list)
# Unknown roles / users with a missing jurisdiction id get NOTHING (fail closed)
# instead of silently seeing the whole database.
# ─────────────────────────────────────────────────────────────────────────────

def _visible_pw_ids(db: Session, user: User):
    """Query of PregnantWoman.id visible to `user` (same rules as the Beneficiaries list)."""
    q = db.query(PregnantWoman.id)
    role = user.role

    if role == "district" and user.district_id is not None:
        return q.filter(PregnantWoman.district_id == user.district_id)

    if role in ("block", "pmsma") and user.block_id is not None:
        # PMSMA works block-wide (same as /pregnant-women/ and /pmsma-sessions/)
        return q.filter(PregnantWoman.block_id == user.block_id)

    if role == "usg_centre" and user.usg_centre_id is not None:
        centre_pw_ids = db.query(USGAppointment.pregnant_woman_id).filter(
            USGAppointment.usg_centre_id == user.usg_centre_id
        ).distinct().subquery()
        return q.filter(PregnantWoman.id.in_(centre_pw_ids))

    if role == "dp" and user.dp_id is not None:
        dp_pw_ids = db.query(DeliveryReferral.pregnant_woman_id).filter(
            DeliveryReferral.dp_id == user.dp_id
        ).distinct().subquery()
        return q.filter(PregnantWoman.id.in_(dp_pw_ids))

    if role == "sub_centre" and user.sub_centre_id is not None:
        user_wards = db.query(UserWardMapping.ward_id).filter(
            UserWardMapping.user_id == user.id
        ).all()
        if user_wards:
            return q.filter(PregnantWoman.ward_id.in_([w.ward_id for w in user_wards]))
        mapped_ward_ids = db.query(WardSubcentreMapping.ward_id).filter(
            WardSubcentreMapping.sub_centre_id == user.sub_centre_id
        ).subquery()
        return q.filter(or_(
            PregnantWoman.ward_id.in_(mapped_ward_ids),
            PregnantWoman.sub_centre_id == user.sub_centre_id
        ))

    return q.filter(false())


def _scoped_usg_query(db: Session, user: User, pw_ids):
    """USG appointments visible to `user` (same rules as the USG Appointment list)."""
    q = db.query(USGAppointment)
    role = user.role
    if role == "usg_centre" and user.usg_centre_id is not None:
        return q.filter(USGAppointment.usg_centre_id == user.usg_centre_id)
    if role == "pmsma":
        return q.filter(USGAppointment.scheduled_by == user.id)
    if role in ("district", "block", "sub_centre", "dp"):
        return q.filter(USGAppointment.pregnant_woman_id.in_(pw_ids))
    return q.filter(false())


def _scoped_grievance_query(db: Session, user: User):
    """Grievances are only handled by district and block users."""
    q = db.query(Grievance)
    if user.role == "district" and user.district_id is not None:
        return q.filter(Grievance.district_id == user.district_id)
    if user.role == "block" and user.block_id is not None:
        return q.filter(Grievance.block_id == user.block_id)
    return q.filter(false())


def _scoped_referral_query(db: Session, user: User, pw_ids):
    """Delivery referrals visible to `user` (same rules as the Delivery Referral list)."""
    q = db.query(DeliveryReferral)
    role = user.role
    if role in ("district", "block"):
        return q.filter(DeliveryReferral.pregnant_woman_id.in_(pw_ids))
    if role == "sub_centre":
        return q.filter(or_(
            DeliveryReferral.sub_centre_id == user.sub_centre_id,
            DeliveryReferral.pregnant_woman_id.in_(pw_ids)
        ))
    if role == "dp" and user.dp_id is not None:
        return q.filter(DeliveryReferral.dp_id == user.dp_id)
    if role == "pmsma":
        return q.filter(DeliveryReferral.referred_by_user_id == user.id)
    return q.filter(false())


def _scoped_pmsma_session_query(db: Session, user: User, pw_ids):
    """PMSMA sessions visible to `user` (same scoping rules as the PMSMA Sessions module)."""
    q = db.query(PMSMASession)
    role = user.role
    if role == "pmsma" and user.block_id is not None:
        q = q.filter(PMSMASession.block_id == user.block_id)
        if user.pmsma_centre_id:
            q = q.filter(PMSMASession.pmsma_centre_id == user.pmsma_centre_id)
        return q
    if role == "sub_centre" or role in ("district", "block"):
        return q.filter(PMSMASession.pregnant_woman_id.in_(pw_ids))
    return q.filter(false())


def _scoped_outcome_query(db: Session, user: User, pw_ids):
    """Delivery outcomes visible to `user`."""
    q = db.query(DeliveryOutcome)
    role = user.role
    if role in ("district", "block", "sub_centre"):
        return q.filter(DeliveryOutcome.pregnant_woman_id.in_(pw_ids))
    if role == "dp" and user.dp_id is not None:
        return q.filter(DeliveryOutcome.dp_id == user.dp_id)
    if role == "pmsma":
        own_ref_pw_ids = db.query(DeliveryReferral.pregnant_woman_id).filter(
            DeliveryReferral.referred_by_user_id == user.id
        ).distinct().subquery()
        return q.filter(DeliveryOutcome.pregnant_woman_id.in_(own_ref_pw_ids))
    return q.filter(false())


router = APIRouter(prefix="/dashboard", tags=["Dashboard & Analytics"])

@router.get("/stats", response_model=DashboardStats)
async def get_dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get dashboard statistics for current user's jurisdiction
    """
    pw_ids = _visible_pw_ids(db, current_user).subquery()

    pw_query = db.query(PregnantWoman).filter(PregnantWoman.id.in_(pw_ids))
    usg_query = _scoped_usg_query(db, current_user, pw_ids)
    grievance_query = _scoped_grievance_query(db, current_user)

    # Calculate statistics
    total_pw = pw_query.count()
    active_pw = pw_query.filter(PregnantWoman.is_active == True).count()
    high_risk = pw_query.filter(PregnantWoman.is_high_risk == True).count()
    active_high_risk = pw_query.filter(PregnantWoman.is_high_risk == True, PregnantWoman.is_active == True).count()

    total_usg = usg_query.count()
    pending_usg = usg_query.filter(USGAppointment.status == "scheduled").count()
    completed_usg = usg_query.filter(USGAppointment.status == "completed").count()
    rescheduled_usg = usg_query.filter(USGAppointment.status == "rescheduled").count()
    accepted_usg = usg_query.filter(USGAppointment.status == "accepted").count()
    cancelled_usg = usg_query.filter(USGAppointment.status == "cancelled").count()
    # Active USG cases = still to be done (scheduled + accepted + rescheduled)
    active_usg = pending_usg + accepted_usg + rescheduled_usg
    # Completion rate = completed / (all appointments except cancelled), 1 decimal
    usg_rate_base = total_usg - cancelled_usg
    usg_completion_rate = round(completed_usg * 100.0 / usg_rate_base, 1) if usg_rate_base > 0 else 0.0

    # PMSMA session stats — scoped by role (relevant to the PMSMA user's own dashboard)
    pmsma_session_query = _scoped_pmsma_session_query(db, current_user, pw_ids)
    total_pmsma_sessions = pmsma_session_query.count()
    pending_pmsma_sessions = pmsma_session_query.filter(
        PMSMASession.status.in_(["scheduled", "rescheduled"])
    ).count()
    active_pmsma_sessions = pmsma_session_query.filter(
        PMSMASession.status.in_(["scheduled", "rescheduled"]),
        PMSMASession.scheduled_date >= datetime.combine(date.today(), datetime.min.time()),
    ).count()
    completed_pmsma_sessions = pmsma_session_query.filter(PMSMASession.status == "completed").count()

    pending_grievances = grievance_query.filter(Grievance.status != "resolved").count()
    resolved_grievances = grievance_query.filter(Grievance.status == "resolved").count()

    # Delivery stats — scoped by role
    ref_query = _scoped_referral_query(db, current_user, pw_ids)
    out_query = _scoped_outcome_query(db, current_user, pw_ids)

    total_referrals = ref_query.count()
    pending_referrals = ref_query.filter(DeliveryReferral.status == "pending").count()
    accepted_referrals = ref_query.filter(DeliveryReferral.status == "accepted").count()
    completed_deliveries = ref_query.filter(DeliveryReferral.status == "completed").count()
    total_outcomes = out_query.count()

    # Maternal & infant mortality — scoped to the same outcomes as out_query above.
    # A maternal death is recorded either via the maternal_outcome field or via the
    # (legacy, still selectable) 'maternal_death' delivery type — count both, once.
    maternal_deaths = out_query.filter(or_(
        DeliveryOutcome.maternal_outcome == MaternalOutcome.MATERNAL_DEATH,
        DeliveryOutcome.delivery_type == DeliveryType.MATERNAL_DEATH,
    )).count()

    outcome_ids_subq = out_query.with_entities(DeliveryOutcome.id).subquery()
    baby_query = db.query(DeliveryOutcomeBaby).filter(
        DeliveryOutcomeBaby.outcome_id.in_(outcome_ids_subq)
    )
    infant_deaths = baby_query.filter(DeliveryOutcomeBaby.status == "infant_death").count()
    still_births = baby_query.filter(DeliveryOutcomeBaby.status == "still_birth").count()

    return DashboardStats(
        total_pregnant_women=total_pw,
        active_pregnant_women=active_pw,
        total_usg_appointments=total_usg,
        pending_usg_appointments=pending_usg,
        completed_usg_appointments=completed_usg,
        rescheduled_usg_appointments=rescheduled_usg,
        active_usg_appointments=active_usg,
        cancelled_usg_appointments=cancelled_usg,
        usg_completion_rate=usg_completion_rate,
        high_risk_cases=high_risk,
        active_high_risk_cases=active_high_risk,
        pending_grievances=pending_grievances,
        resolved_grievances=resolved_grievances,
        total_referrals=total_referrals,
        pending_referrals=pending_referrals,
        accepted_referrals=accepted_referrals,
        completed_deliveries=completed_deliveries,
        total_outcomes=total_outcomes,
        maternal_deaths=maternal_deaths,
        infant_deaths=infant_deaths,
        still_births=still_births,
        total_pmsma_sessions=total_pmsma_sessions,
        pending_pmsma_sessions=pending_pmsma_sessions,
        active_pmsma_sessions=active_pmsma_sessions,
        completed_pmsma_sessions=completed_pmsma_sessions,
    )

@router.get("/district-overview", response_model=DistrictDashboardStats)
async def get_district_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get comprehensive district-level dashboard (District admin only)
    """
    if current_user.role != "district":
        raise HTTPException(
            status_code=403,
            detail="Only district users can access this endpoint"
        )
    
    # District-level counts
    total_blocks = db.query(Block).filter(
        Block.district_id == current_user.district_id,
        Block.is_active == True
    ).count()
    
    total_sub_centres = db.query(SubCentre).join(Block).filter(
        Block.district_id == current_user.district_id,
        SubCentre.is_active == True
    ).count()
    
    total_usg_centres = db.query(USGCentre).filter(
        USGCentre.district_id == current_user.district_id,
        USGCentre.is_active == True
    ).count()
    
    # Overall district stats
    district_pw = db.query(PregnantWoman).filter(
        PregnantWoman.district_id == current_user.district_id
    )
    
    district_usg = db.query(USGAppointment).join(PregnantWoman).filter(
        PregnantWoman.district_id == current_user.district_id
    )
    
    district_grievances = db.query(Grievance).filter(
        Grievance.district_id == current_user.district_id
    )
    
    overall_stats = DashboardStats(
        total_pregnant_women=district_pw.count(),
        active_pregnant_women=district_pw.filter(PregnantWoman.is_active == True).count(),
        total_usg_appointments=district_usg.count(),
        pending_usg_appointments=district_usg.filter(USGAppointment.status == "scheduled").count(),
        completed_usg_appointments=district_usg.filter(USGAppointment.status == "completed").count(),
        high_risk_cases=district_pw.filter(PregnantWoman.is_high_risk == True).count(),
        pending_grievances=district_grievances.filter(Grievance.status != "resolved").count(),
        resolved_grievances=district_grievances.filter(Grievance.status == "resolved").count()
    )
    
    # Block-wise statistics
    blocks = db.query(Block).filter(
        Block.district_id == current_user.district_id,
        Block.is_active == True
    ).all()
    
    block_stats_list = []
    for block in blocks:
        block_pw = db.query(PregnantWoman).filter(PregnantWoman.block_id == block.id)
        block_usg = db.query(USGAppointment).join(PregnantWoman).filter(
            PregnantWoman.block_id == block.id
        )
        block_grievances = db.query(Grievance).filter(Grievance.block_id == block.id)
        
        block_sub_centres = db.query(SubCentre).filter(
            SubCentre.block_id == block.id,
            SubCentre.is_active == True
        ).count()
        
        block_stats = BlockDashboardStats(
            block_id=block.id,
            block_name=block.name,
            total_sub_centres=block_sub_centres,
            total_pregnant_women=block_pw.count(),
            active_pregnant_women=block_pw.filter(PregnantWoman.is_active == True).count(),
            total_usg_appointments=block_usg.count(),
            pending_usg_appointments=block_usg.filter(USGAppointment.status == "scheduled").count(),
            completed_usg_appointments=block_usg.filter(USGAppointment.status == "completed").count(),
            high_risk_cases=block_pw.filter(PregnantWoman.is_high_risk == True).count(),
            pending_grievances=block_grievances.filter(Grievance.status != "resolved").count(),
            resolved_grievances=block_grievances.filter(Grievance.status == "resolved").count()
        )
        block_stats_list.append(block_stats)
    
    return DistrictDashboardStats(
        total_blocks=total_blocks,
        total_sub_centres=total_sub_centres,
        total_usg_centres=total_usg_centres,
        overall_stats=overall_stats,
        block_wise_stats=block_stats_list
    )

@router.get("/analytics/monthly-trends")
async def get_monthly_trends(
    year: int = datetime.now().year,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get monthly trends for registrations, appointments, and grievances
    """
    pw_ids = _visible_pw_ids(db, current_user).subquery()
    pw_query = db.query(PregnantWoman).filter(PregnantWoman.id.in_(pw_ids))
    usg_query = _scoped_usg_query(db, current_user, pw_ids)
    grievance_query = _scoped_grievance_query(db, current_user)

    # Monthly registrations
    monthly_registrations = pw_query.with_entities(
        extract('month', PregnantWoman.created_at).label('month'),
        func.count(PregnantWoman.id).label('count')
    ).filter(
        extract('year', PregnantWoman.created_at) == year
    ).group_by('month').all()

    # Monthly USG appointments
    monthly_usg = usg_query.with_entities(
        extract('month', USGAppointment.created_at).label('month'),
        func.count(USGAppointment.id).label('count')
    ).filter(
        extract('year', USGAppointment.created_at) == year
    ).group_by('month').all()

    # Monthly grievances
    monthly_grievances = grievance_query.with_entities(
        extract('month', Grievance.created_at).label('month'),
        func.count(Grievance.id).label('count')
    ).filter(
        extract('year', Grievance.created_at) == year
    ).group_by('month').all()

    # Format response
    months_data = []
    for month in range(1, 13):
        reg_count = next((r.count for r in monthly_registrations if r.month == month), 0)
        usg_count = next((u.count for u in monthly_usg if u.month == month), 0)
        grv_count = next((g.count for g in monthly_grievances if g.month == month), 0)

        months_data.append({
            "month": month,
            "registrations": reg_count,
            "usg_appointments": usg_count,
            "grievances": grv_count
        })

    return {
        "year": year,
        "monthly_data": months_data
    }

@router.get("/analytics/high-risk-analysis")
async def get_high_risk_analysis(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get analysis of high-risk pregnancy cases
    """
    pw_ids = _visible_pw_ids(db, current_user).subquery()
    query = db.query(PregnantWoman).filter(
        PregnantWoman.id.in_(pw_ids),
        PregnantWoman.is_high_risk == True
    )

    total_high_risk = query.count()
    
    # High risk by block (for district users)
    block_wise = []
    if current_user.role == "district":
        blocks = db.query(Block).filter(
            Block.district_id == current_user.district_id,
            Block.is_active == True
        ).all()
        
        for block in blocks:
            count = db.query(PregnantWoman).filter(
                PregnantWoman.block_id == block.id,
                PregnantWoman.is_high_risk == True
            ).count()
            
            block_wise.append({
                "block_id": block.id,
                "block_name": block.name,
                "high_risk_count": count
            })
    
    # High risk with completed USG
    # (distinct: a woman with several completed scans must be counted once)
    with_usg = query.join(USGAppointment).filter(
        USGAppointment.status == "completed"
    ).with_entities(func.count(func.distinct(PregnantWoman.id))).scalar() or 0
    
    # High risk without USG
    without_usg = total_high_risk - with_usg
    
    return {
        "total_high_risk": total_high_risk,
        "with_completed_usg": with_usg,
        "without_usg": without_usg,
        "block_wise_distribution": block_wise
    }

@router.get("/analytics/usg-performance")
async def get_usg_performance(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get USG centre performance metrics
    """
    pw_ids = _visible_pw_ids(db, current_user).subquery()
    query = _scoped_usg_query(db, current_user, pw_ids)

    if start_date:
        query = query.filter(USGAppointment.created_at >= start_date)
    if end_date:
        query = query.filter(USGAppointment.created_at <= end_date)
    
    total_appointments = query.count()
    completed = query.filter(USGAppointment.status == "completed").count()
    pending = query.filter(USGAppointment.status == "scheduled").count()
    rescheduled = query.filter(USGAppointment.reschedule_count > 0).count()
    emergency = query.filter(USGAppointment.appointment_type == "emergency").count()
    
    # Average time to completion
    completed_appointments = query.filter(
        USGAppointment.status == "completed",
        USGAppointment.completed_date.isnot(None)
    ).all()
    
    avg_completion_time = 0
    if completed_appointments:
        total_time = sum([
            (appt.completed_date - appt.created_at).days 
            for appt in completed_appointments
        ])
        avg_completion_time = total_time / len(completed_appointments)
    
    return {
        "total_appointments": total_appointments,
        "completed": completed,
        "pending": pending,
        "rescheduled": rescheduled,
        "emergency_cases": emergency,
        "completion_rate": round((completed / total_appointments * 100) if total_appointments > 0 else 0, 2),
        "avg_completion_time_days": round(avg_completion_time, 2)
    }

@router.get("/analytics/block-wise-trends")
async def get_block_wise_trends(
    year: int = datetime.now().year,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get block-wise performance trends (District users only)
    """
    if current_user.role != "district":
        raise HTTPException(
            status_code=403,
            detail="Only district users can access this endpoint"
        )
    
    blocks = db.query(Block).filter(
        Block.district_id == current_user.district_id,
        Block.is_active == True
    ).all()
    
    blocks_data = []
    for block in blocks:
        # Monthly registrations for this block
        monthly_reg = db.query(
            extract('month', PregnantWoman.created_at).label('month'),
            func.count(PregnantWoman.id).label('count')
        ).filter(
            PregnantWoman.block_id == block.id,
            extract('year', PregnantWoman.created_at) == year
        ).group_by('month').all()
        
        # Monthly USG appointments for this block
        monthly_usg = db.query(
            extract('month', USGAppointment.created_at).label('month'),
            func.count(USGAppointment.id).label('count')
        ).join(PregnantWoman).filter(
            PregnantWoman.block_id == block.id,
            extract('year', USGAppointment.created_at) == year
        ).group_by('month').all()
        
        # Monthly grievances for this block
        monthly_grv = db.query(
            extract('month', Grievance.created_at).label('month'),
            func.count(Grievance.id).label('count')
        ).filter(
            Grievance.block_id == block.id,
            extract('year', Grievance.created_at) == year
        ).group_by('month').all()
        
        # Format monthly data
        monthly_data = []
        for month in range(1, 13):
            reg_count = next((r.count for r in monthly_reg if r.month == month), 0)
            usg_count = next((u.count for u in monthly_usg if u.month == month), 0)
            grv_count = next((g.count for g in monthly_grv if g.month == month), 0)
            
            monthly_data.append({
                "month": month,
                "registrations": reg_count,
                "appointments": usg_count,
                "grievances": grv_count
            })
        
        blocks_data.append({
            "block_id": block.id,
            "block_name": block.name,
            "monthly_data": monthly_data
        })
    
    return {
        "year": year,
        "blocks": blocks_data
    }

@router.get("/analytics/ward-wise-trends")
async def get_ward_wise_trends(
    block_id: int,
    year: int = datetime.now().year,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get ward-wise performance trends for a specific block
    """
    # Verify block access
    block = db.query(Block).filter(Block.id == block_id).first()
    if not block:
        raise HTTPException(status_code=404, detail="Block not found")
    
    if current_user.role == "district" and block.district_id != current_user.district_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    elif current_user.role == "block" and block.id != current_user.block_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    from models import Ward
    wards = db.query(Ward).filter(
        Ward.block_id == block_id,
        Ward.is_active == True
    ).all()
    
    wards_data = []
    for ward in wards:
        # Monthly registrations for this ward
        monthly_reg = db.query(
            extract('month', PregnantWoman.created_at).label('month'),
            func.count(PregnantWoman.id).label('count')
        ).filter(
            PregnantWoman.ward_id == ward.id,
            extract('year', PregnantWoman.created_at) == year
        ).group_by('month').all()
        
        # Monthly USG appointments for this ward
        monthly_usg = db.query(
            extract('month', USGAppointment.created_at).label('month'),
            func.count(USGAppointment.id).label('count')
        ).join(PregnantWoman).filter(
            PregnantWoman.ward_id == ward.id,
            extract('year', USGAppointment.created_at) == year
        ).group_by('month').all()
        
        # Monthly grievances for this ward
        monthly_grv = db.query(
            extract('month', Grievance.created_at).label('month'),
            func.count(Grievance.id).label('count')
        ).filter(
            Grievance.ward_id == ward.id,
            extract('year', Grievance.created_at) == year
        ).group_by('month').all()
        
        # Format monthly data
        monthly_data = []
        for month in range(1, 13):
            reg_count = next((r.count for r in monthly_reg if r.month == month), 0)
            usg_count = next((u.count for u in monthly_usg if u.month == month), 0)
            grv_count = next((g.count for g in monthly_grv if g.month == month), 0)
            
            monthly_data.append({
                "month": month,
                "registrations": reg_count,
                "appointments": usg_count,
                "grievances": grv_count
            })
        
        wards_data.append({
            "ward_id": ward.id,
            "ward_name": ward.name,
            "monthly_data": monthly_data
        })
    
    return {
        "year": year,
        "block_id": block_id,
        "block_name": block.name,
        "wards": wards_data
    }

@router.get("/notifications/unread-count")
async def get_unread_notifications_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get count of unread notifications for current user
    """
    from models import Notification
    
    count = db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.is_read == False
    ).count()
    
    return {"unread_count": count}

@router.get("/quick-stats")
async def get_quick_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get quick statistics for mobile app dashboard
    """
    if current_user.role == "usg_centre":
        pending_appointments = db.query(USGAppointment).filter(
            USGAppointment.usg_centre_id == current_user.usg_centre_id,
            USGAppointment.status == "scheduled"
        ).count()
        
        today_appointments = db.query(USGAppointment).filter(
            USGAppointment.usg_centre_id == current_user.usg_centre_id,
            func.date(USGAppointment.scheduled_date) == date.today()
        ).count()
        
        return {
            "pending_appointments": pending_appointments,
            "today_appointments": today_appointments
        }
    
    elif current_user.role == "sub_centre":
        pw_ids = _visible_pw_ids(db, current_user).subquery()
        active_pw = db.query(PregnantWoman).filter(
            PregnantWoman.id.in_(pw_ids),
            PregnantWoman.is_active == True
        ).count()

        pending_approvals = db.query(PregnantWoman).filter(
            PregnantWoman.id.in_(pw_ids),
            PregnantWoman.is_self_registered == True,
            PregnantWoman.registration_approved == False
        ).count()

        # ANC visits due today
        anc_due_today = db.query(ANCVisit).filter(
            ANCVisit.pregnant_woman_id.in_(pw_ids),
            ANCVisit.next_visit_date == date.today()
        ).count()

        return {
            "active_pregnant_women": active_pw,
            "pending_approvals": pending_approvals,
            "anc_visits_due_today": anc_due_today
        }

    elif current_user.role == "dp":
        pending_referrals = db.query(DeliveryReferral).filter(
            DeliveryReferral.dp_id == current_user.dp_id,
            DeliveryReferral.status == "pending"
        ).count()
        accepted_referrals = db.query(DeliveryReferral).filter(
            DeliveryReferral.dp_id == current_user.dp_id,
            DeliveryReferral.status == "accepted"
        ).count()
        completed_referrals = db.query(DeliveryReferral).filter(
            DeliveryReferral.dp_id == current_user.dp_id,
            DeliveryReferral.status == "completed"
        ).count()
        return {
            "pending_referrals": pending_referrals,
            "accepted_referrals": accepted_referrals,
            "completed_referrals": completed_referrals,
        }
    
    else:
        return await get_dashboard_stats(db, current_user)

@router.get("/analytics/anc-statistics")
async def get_anc_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get ANC visit statistics for dashboard
    """
    pw_ids = _visible_pw_ids(db, current_user).subquery()
    query = db.query(ANCVisit).filter(ANCVisit.pregnant_woman_id.in_(pw_ids))

    total_visits = query.count()
    
    # This month's visits
    today = date.today()
    first_day = today.replace(day=1)
    this_month = query.filter(ANCVisit.visit_date >= first_day).count()
    
    # PMSMA referrals (column is still named referred_for_usg)
    usg_referrals = query.filter(ANCVisit.referred_for_usg == True).count()
    
    # Emergency visits
    emergency_visits = query.filter(ANCVisit.is_emergency == True).count()
    
    return {
        "total_anc_visits": total_visits,
        "visits_this_month": this_month,
        "usg_referrals": usg_referrals,
        "emergency_visits": emergency_visits
    }

# BLOCK USER ENHANCED DASHBOARD ENDPOINTS

@router.get("/block-overview")
async def get_block_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Comprehensive block-level dashboard — Block user only"""
    if current_user.role != "block":
        raise HTTPException(status_code=403, detail="Block user access required")

    from models import Ward, USGCentreBlockMapping, DeliveryPoint

    block_id = current_user.block_id

    # ── Part 1: Infrastructure counts ────────────────────────────────────────
    total_sub_centres = db.query(SubCentre).filter(
        SubCentre.block_id == block_id,
        SubCentre.is_active == True
    ).count()

    total_wards = db.query(Ward).filter(
        Ward.block_id == block_id,
        Ward.is_active == True
    ).count()

    total_usg_centres = db.query(USGCentreBlockMapping).filter(
        USGCentreBlockMapping.block_id == block_id
    ).count()

    total_dps = db.query(DeliveryPoint).filter(
        DeliveryPoint.block_id == block_id,
        DeliveryPoint.is_active == True
    ).count()

    # ── Part 2: Overall block stats ───────────────────────────────────────────
    pw_q = db.query(PregnantWoman).filter(PregnantWoman.block_id == block_id)
    usg_q = db.query(USGAppointment).join(PregnantWoman).filter(PregnantWoman.block_id == block_id)
    grv_q = db.query(Grievance).filter(Grievance.block_id == block_id)

    ref_pw_ids = db.query(PregnantWoman.id).filter(PregnantWoman.block_id == block_id).subquery()
    ref_q = db.query(DeliveryReferral).filter(DeliveryReferral.pregnant_woman_id.in_(ref_pw_ids))
    out_q = db.query(DeliveryOutcome).filter(DeliveryOutcome.pregnant_woman_id.in_(ref_pw_ids))

    overall_stats = {
        "total_pregnant_women": pw_q.count(),
        "active_pregnant_women": pw_q.filter(PregnantWoman.is_active == True).count(),
        "high_risk_cases": pw_q.filter(PregnantWoman.is_high_risk == True).count(),
        "total_usg_appointments": usg_q.count(),
        "pending_usg_appointments": usg_q.filter(USGAppointment.status == "scheduled").count(),
        "completed_usg_appointments": usg_q.filter(USGAppointment.status == "completed").count(),
        "pending_grievances": grv_q.filter(Grievance.status != "resolved").count(),
        "resolved_grievances": grv_q.filter(Grievance.status == "resolved").count(),
        "total_referrals": ref_q.count(),
        "pending_referrals": ref_q.filter(DeliveryReferral.status == "pending").count(),
        "completed_deliveries": ref_q.filter(DeliveryReferral.status == "completed").count(),
        "total_outcomes": out_q.count(),
    }

    # ── Part 3: Sub-centre wise stats ─────────────────────────────────────────
    sub_centres = db.query(SubCentre).filter(
        SubCentre.block_id == block_id,
        SubCentre.is_active == True
    ).all()

    sub_centre_wise_stats = []
    for sc in sub_centres:
        sc_pw = db.query(PregnantWoman).filter(PregnantWoman.sub_centre_id == sc.id)
        sc_usg = db.query(USGAppointment).join(PregnantWoman).filter(PregnantWoman.sub_centre_id == sc.id)
        sc_anc = db.query(ANCVisit).join(PregnantWoman).filter(PregnantWoman.sub_centre_id == sc.id)
        sc_ref_pw_ids = db.query(PregnantWoman.id).filter(PregnantWoman.sub_centre_id == sc.id).subquery()
        sc_ref = db.query(DeliveryReferral).filter(DeliveryReferral.pregnant_woman_id.in_(sc_ref_pw_ids))

        sub_centre_wise_stats.append({
            "sub_centre_id": sc.id,
            "sub_centre_name": sc.name,
            "total_pregnant_women": sc_pw.count(),
            "active_pregnant_women": sc_pw.filter(PregnantWoman.is_active == True).count(),
            "high_risk_cases": sc_pw.filter(PregnantWoman.is_high_risk == True).count(),
            "total_anc_visits": sc_anc.count(),
            "total_usg_appointments": sc_usg.count(),
            "pending_usg_appointments": sc_usg.filter(USGAppointment.status == "scheduled").count(),
            "total_referrals": sc_ref.count(),
        })

    # ── Part 4: Ward wise stats ───────────────────────────────────────────────
    wards = db.query(Ward).filter(
        Ward.block_id == block_id,
        Ward.is_active == True
    ).all()

    ward_wise_stats = []
    for ward in wards:
        w_pw = db.query(PregnantWoman).filter(PregnantWoman.ward_id == ward.id)
        w_usg = db.query(USGAppointment).join(PregnantWoman).filter(PregnantWoman.ward_id == ward.id)
        w_ref_pw_ids = db.query(PregnantWoman.id).filter(PregnantWoman.ward_id == ward.id).subquery()
        w_ref = db.query(DeliveryReferral).filter(DeliveryReferral.pregnant_woman_id.in_(w_ref_pw_ids))
        ward_wise_stats.append({
            "ward_id": ward.id,
            "ward_name": ward.name,
            "total_pregnant_women": w_pw.count(),
            "active_pregnant_women": w_pw.filter(PregnantWoman.is_active == True).count(),
            "high_risk_cases": w_pw.filter(PregnantWoman.is_high_risk == True).count(),
            "total_usg_appointments": w_usg.count(),
            "pending_usg_appointments": w_usg.filter(USGAppointment.status == "scheduled").count(),
            "total_referrals": w_ref.count(),
        })

    return {
        "total_sub_centres": total_sub_centres,
        "total_wards": total_wards,
        "total_usg_centres": total_usg_centres,
        "total_dps": total_dps,
        "overall_stats": overall_stats,
        "sub_centre_wise_stats": sub_centre_wise_stats,
        "ward_wise_stats": ward_wise_stats,
    }


# BLOCK USER ENHANCED DASHBOARD ENDPOINTS
@router.get("/block/ward-comparison")
async def get_block_ward_comparison(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Chart data: Ward-wise pregnant women comparison (Bar Chart)"""
    if current_user.role != "block":
        raise HTTPException(status_code=403, detail="Block user access required")
    
    from models import Ward
    from sqlalchemy import case
    
    data = db.query(
        func.coalesce(Ward.name, 'Rural Area').label('ward'),
        func.count(PregnantWoman.id).label('total'),
        func.sum(case((PregnantWoman.is_high_risk == True, 1), else_=0)).label('high_risk')
    ).outerjoin(Ward, PregnantWoman.ward_id == Ward.id)\
     .filter(PregnantWoman.block_id == current_user.block_id)\
     .group_by(Ward.name).all()
    
    return {
        "labels": [d.ward for d in data],
        "datasets": [
            {"label": "Total", "data": [d.total for d in data]},
            {"label": "High Risk", "data": [d.high_risk for d in data]}
        ]
    }

@router.get("/block/subcentre-performance")
async def get_subcentre_performance(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Chart data: Sub-centre performance metrics (Horizontal Bar Chart)"""
    if current_user.role != "block":
        raise HTTPException(status_code=403, detail="Block user access required")
    
    data = db.query(
        SubCentre.name,
        func.count(func.distinct(PregnantWoman.id)).label('women_count'),
        func.count(func.distinct(ANCVisit.id)).label('anc_count')
    ).outerjoin(PregnantWoman, SubCentre.id == PregnantWoman.sub_centre_id)\
     .outerjoin(ANCVisit, PregnantWoman.id == ANCVisit.pregnant_woman_id)\
     .filter(SubCentre.block_id == current_user.block_id)\
     .group_by(SubCentre.id, SubCentre.name).all()
    
    return {
        "labels": [d.name for d in data],
        "datasets": [
            {"label": "Pregnant Women", "data": [d.women_count for d in data]},
            {"label": "ANC Visits", "data": [d.anc_count for d in data]}
        ]
    }

@router.get("/block/usg-status-breakdown")
async def get_usg_status_breakdown(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Chart data: USG appointment status (Donut/Pie Chart)"""
    if current_user.role != "block":
        raise HTTPException(status_code=403, detail="Block user access required")
    
    data = db.query(
        USGAppointment.status,
        func.count(USGAppointment.id).label('count')
    ).join(PregnantWoman)\
     .filter(PregnantWoman.block_id == current_user.block_id)\
     .group_by(USGAppointment.status).all()
    
    return {
        "labels": [d.status.title() for d in data],
        "data": [d.count for d in data]
    }

@router.get("/block/monthly-registrations")
async def get_block_monthly_registrations(
    year: int = datetime.now().year,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Chart data: Monthly registration trends (Line Chart)"""
    if current_user.role != "block":
        raise HTTPException(status_code=403, detail="Block user access required")
    
    data = db.query(
        extract('month', PregnantWoman.created_at).label('month'),
        func.count(PregnantWoman.id).label('count')
    ).filter(
        PregnantWoman.block_id == current_user.block_id,
        extract('year', PregnantWoman.created_at) == year
    ).group_by('month').all()
    
    monthly_counts = [0] * 12
    for d in data:
        monthly_counts[int(d.month) - 1] = d.count
    
    return {
        "labels": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        "data": monthly_counts
    }

@router.get("/block/grievance-trends")
async def get_block_grievance_trends(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Chart data: Grievance status distribution (Stacked Bar Chart)"""
    if current_user.role != "block":
        raise HTTPException(status_code=403, detail="Block user access required")
    
    from sqlalchemy import case
    
    data = db.query(
        func.count(Grievance.id).label('total'),
        func.sum(case((Grievance.status == 'pending', 1), else_=0)).label('pending'),
        func.sum(case((Grievance.status == 'in_progress', 1), else_=0)).label('in_progress'),
        func.sum(case((Grievance.status == 'resolved', 1), else_=0)).label('resolved'),
        func.sum(case((Grievance.escalated_to_district == True, 1), else_=0)).label('escalated')
    ).filter(Grievance.block_id == current_user.block_id).first()
    
    return {
        "labels": ["Grievances"],
        "datasets": [
            {"label": "Pending", "data": [data.pending]},
            {"label": "In Progress", "data": [data.in_progress]},
            {"label": "Resolved", "data": [data.resolved]},
            {"label": "Escalated", "data": [data.escalated]}
        ]
    }

@router.get("/block/high-risk-timeline")
async def get_high_risk_timeline(
    months: int = 6,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Chart data: High-risk cases over time (Area Chart)"""
    if current_user.role != "block":
        raise HTTPException(status_code=403, detail="Block user access required")
    
    from datetime import timedelta
    end_date = date.today()
    start_date = end_date - timedelta(days=30 * months)
    
    data = db.query(
        extract('month', PregnantWoman.created_at).label('month'),
        func.count(PregnantWoman.id).label('total'),
        func.sum(case((PregnantWoman.is_high_risk == True, 1), else_=0)).label('high_risk')
    ).filter(
        PregnantWoman.block_id == current_user.block_id,
        PregnantWoman.created_at >= start_date
    ).group_by('month').all()
    
    return {
        "labels": [f"Month {int(d.month)}" for d in data],
        "datasets": [
            {"label": "Total Cases", "data": [d.total for d in data]},
            {"label": "High Risk", "data": [d.high_risk for d in data]}
        ]
    }

@router.get("/analytics/daily-trends")
async def get_daily_trends(
    month: int,
    year: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get daily trends for a specific month"""
    from calendar import monthrange
    
    # Role-based filtering
    if current_user.role == "district":
        pw_filter = PregnantWoman.district_id == current_user.district_id
        usg_filter = PregnantWoman.district_id == current_user.district_id
        grv_filter = Grievance.district_id == current_user.district_id
    elif current_user.role == "block":
        pw_filter = PregnantWoman.block_id == current_user.block_id
        usg_filter = PregnantWoman.block_id == current_user.block_id
        grv_filter = Grievance.block_id == current_user.block_id
    else:
        # sub_centre / usg_centre / dp / pmsma — same visibility as their own lists
        _ids = _visible_pw_ids(db, current_user).subquery()
        pw_filter = PregnantWoman.id.in_(_ids)
        usg_filter = PregnantWoman.id.in_(_ids)
        grv_filter = false()   # these roles have no grievance access
    
    # Daily registrations
    daily_reg = db.query(
        extract('day', PregnantWoman.created_at).label('day'),
        func.count(PregnantWoman.id).label('count')
    ).filter(
        extract('year', PregnantWoman.created_at) == year,
        extract('month', PregnantWoman.created_at) == month,
        pw_filter
    ).group_by('day').all()
    
    # Daily USG appointments
    daily_usg = db.query(
        extract('day', USGAppointment.created_at).label('day'),
        func.count(USGAppointment.id).label('count')
    ).join(PregnantWoman).filter(
        extract('year', USGAppointment.created_at) == year,
        extract('month', USGAppointment.created_at) == month,
        usg_filter
    ).group_by('day').all()
    
    # Daily grievances
    daily_grv_query = db.query(
        extract('day', Grievance.created_at).label('day'),
        func.count(Grievance.id).label('count')
    ).filter(
        extract('year', Grievance.created_at) == year,
        extract('month', Grievance.created_at) == month
    )
    if grv_filter is not None:
        daily_grv_query = daily_grv_query.filter(grv_filter)
    daily_grv = daily_grv_query.group_by('day').all()
    
    # Get number of days in month
    days_in_month = monthrange(year, month)[1]
    
    # Format response
    daily_data = []
    for day in range(1, days_in_month + 1):
        reg_count = next((r.count for r in daily_reg if r.day == day), 0)
        usg_count = next((u.count for u in daily_usg if u.day == day), 0)
        grv_count = next((g.count for g in daily_grv if g.day == day), 0)
        
        daily_data.append({
            "day": day,
            "registrations": reg_count,
            "usg_appointments": usg_count,
            "grievances": grv_count
        })
    
    return {
        "month": month,
        "year": year,
        "daily_data": daily_data
    }

@router.get("/analytics/weekly-trends")
async def get_weekly_trends(
    year: int = datetime.now().year,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get weekly trends for entire year"""
    # Role-based filtering
    if current_user.role == "district":
        pw_filter = PregnantWoman.district_id == current_user.district_id
        usg_filter = PregnantWoman.district_id == current_user.district_id
        grv_filter = Grievance.district_id == current_user.district_id
    elif current_user.role == "block":
        pw_filter = PregnantWoman.block_id == current_user.block_id
        usg_filter = PregnantWoman.block_id == current_user.block_id
        grv_filter = Grievance.block_id == current_user.block_id
    else:
        # sub_centre / usg_centre / dp / pmsma — same visibility as their own lists
        _ids = _visible_pw_ids(db, current_user).subquery()
        pw_filter = PregnantWoman.id.in_(_ids)
        usg_filter = PregnantWoman.id.in_(_ids)
        grv_filter = false()   # these roles have no grievance access
    
    # Weekly registrations
    weekly_reg = db.query(
        extract('week', PregnantWoman.created_at).label('week'),
        func.count(PregnantWoman.id).label('count')
    ).filter(
        extract('year', PregnantWoman.created_at) == year,
        pw_filter
    ).group_by('week').all()
    
    # Weekly USG appointments
    weekly_usg = db.query(
        extract('week', USGAppointment.created_at).label('week'),
        func.count(USGAppointment.id).label('count')
    ).join(PregnantWoman).filter(
        extract('year', USGAppointment.created_at) == year,
        usg_filter
    ).group_by('week').all()
    
    # Weekly grievances
    weekly_grv_query = db.query(
        extract('week', Grievance.created_at).label('week'),
        func.count(Grievance.id).label('count')
    ).filter(
        extract('year', Grievance.created_at) == year
    )
    if grv_filter is not None:
        weekly_grv_query = weekly_grv_query.filter(grv_filter)
    weekly_grv = weekly_grv_query.group_by('week').all()
    
    # Format response (52 weeks)
    weekly_data = []
    for week in range(1, 53):
        reg_count = next((r.count for r in weekly_reg if r.week == week), 0)
        usg_count = next((u.count for u in weekly_usg if u.week == week), 0)
        grv_count = next((g.count for g in weekly_grv if g.week == week), 0)
        
        weekly_data.append({
            "week": week,
            "registrations": reg_count,
            "usg_appointments": usg_count,
            "grievances": grv_count
        })
    
    return {
        "year": year,
        "weekly_data": weekly_data
    }

@router.get("/analytics/today-stats")
async def get_today_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get today's statistics"""
    today = date.today()
    
    # Role-based filtering
    pw_ids = _visible_pw_ids(db, current_user).subquery()
    pw_query = db.query(PregnantWoman).filter(PregnantWoman.id.in_(pw_ids))
    usg_query = _scoped_usg_query(db, current_user, pw_ids)
    grievance_query = _scoped_grievance_query(db, current_user)
    anc_query = db.query(ANCVisit).filter(ANCVisit.pregnant_woman_id.in_(pw_ids))

    # Today's counts
    registrations_today = pw_query.filter(func.date(PregnantWoman.created_at) == today).count()
    usg_appointments_today = usg_query.filter(func.date(USGAppointment.created_at) == today).count()
    grievances_today = grievance_query.filter(func.date(Grievance.created_at) == today).count()
    anc_visits_today = anc_query.filter(ANCVisit.visit_date == today).count()
    
    # Scheduled for today
    usg_scheduled_today = usg_query.filter(func.date(USGAppointment.scheduled_date) == today).count()
    anc_due_today = anc_query.filter(ANCVisit.next_visit_date == today).count()
    
    return {
        "date": today,
        "registrations_today": registrations_today,
        "usg_appointments_created_today": usg_appointments_today,
        "usg_scheduled_today": usg_scheduled_today,
        "grievances_today": grievances_today,
        "anc_visits_today": anc_visits_today,
        "anc_due_today": anc_due_today
    }


# DISTRICT USER ENHANCED DASHBOARD ENDPOINTS

@router.get("/district/grievance-trends")
async def get_district_grievance_trends(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Chart data: Block-wise grievance breakdown for district (Stacked Bar Chart)"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="District user access required")

    from sqlalchemy import case

    blocks = db.query(Block).filter(
        Block.district_id == current_user.district_id,
        Block.is_active == True
    ).all()

    labels = []
    pending_data = []
    in_progress_data = []
    resolved_data = []
    escalated_data = []

    for block in blocks:
        row = db.query(
            func.sum(case((Grievance.status == 'pending', 1), else_=0)).label('pending'),
            func.sum(case((Grievance.status == 'in_progress', 1), else_=0)).label('in_progress'),
            func.sum(case((Grievance.status == 'resolved', 1), else_=0)).label('resolved'),
            func.sum(case((Grievance.escalated_to_district == True, 1), else_=0)).label('escalated')
        ).filter(Grievance.block_id == block.id).first()

        labels.append(block.name)
        pending_data.append(row.pending or 0)
        in_progress_data.append(row.in_progress or 0)
        resolved_data.append(row.resolved or 0)
        escalated_data.append(row.escalated or 0)

    return {
        "labels": labels,
        "datasets": [
            {"label": "Pending", "data": pending_data},
            {"label": "In Progress", "data": in_progress_data},
            {"label": "Resolved", "data": resolved_data},
            {"label": "Escalated", "data": escalated_data}
        ]
    }


@router.get("/district/high-risk-timeline")
async def get_district_high_risk_timeline(
    months: int = 6,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Chart data: High-risk cases over last N months for district (Area Chart)"""
    if current_user.role != "district":
        raise HTTPException(status_code=403, detail="District user access required")

    from datetime import timedelta
    from sqlalchemy import case

    end_date = date.today()
    start_date = end_date - timedelta(days=30 * months)

    data = db.query(
        extract('month', PregnantWoman.created_at).label('month'),
        extract('year', PregnantWoman.created_at).label('year'),
        func.count(PregnantWoman.id).label('total'),
        func.sum(case((PregnantWoman.is_high_risk == True, 1), else_=0)).label('high_risk')
    ).filter(
        PregnantWoman.district_id == current_user.district_id,
        PregnantWoman.created_at >= start_date
    ).group_by(
        extract('year', PregnantWoman.created_at),
        extract('month', PregnantWoman.created_at)
    ).order_by(
        extract('year', PregnantWoman.created_at),
        extract('month', PregnantWoman.created_at)
    ).all()

    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    return {
        "labels": [f"{month_names[int(d.month) - 1]} {int(d.year)}" for d in data],
        "datasets": [
            {"label": "Total Cases", "data": [d.total for d in data]},
            {"label": "High Risk", "data": [d.high_risk for d in data]}
        ]
    }