"""
Shared filter helpers for the District dashboard analytics and the Reports module.

Why this exists
---------------
Both the "Advanced Analytics" dashboard and the Reports tabs need the *same*
beneficiary filters (block / sub-centre / ward, risk, age band, trimester,
parity, blood group, ...).  Keeping them in one place guarantees a filter means
exactly the same thing on the dashboard and in a report.

Everything here returns plain SQLAlchemy *conditions* (not queries) so the same
filter can be used in a plain query, a JOIN, or an IN (subquery).

All date arithmetic is done in Python and passed to SQL as bound dates, so the
code is portable between MySQL/MariaDB (production) and SQLite (tests).
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional, Tuple

from fastapi import Query
from sqlalchemy import and_, or_, false, exists, select
from sqlalchemy.orm import aliased

from models import (
    PregnantWoman, ANCVisit, WardSubcentreMapping, UserWardMapping, User,
)

# ── Constants ────────────────────────────────────────────────────────────────

# key -> (min_age, max_age) inclusive; None = open ended
AGE_BANDS = {
    "lt18": (None, 17),
    "18_24": (18, 24),
    "25_29": (25, 29),
    "30_34": (30, 34),
    "35_plus": (35, None),
}
AGE_BAND_LABELS = {
    "lt18": "Under 18",
    "18_24": "18–24",
    "25_29": "25–29",
    "30_34": "30–34",
    "35_plus": "35+",
    "unknown": "Age not recorded",
}

# Gestational-age cut-offs in days (WHO convention: T1 <14w, T2 14w–27w6d, T3 28w+)
T2_START_DAYS = 14 * 7
T3_START_DAYS = 28 * 7
FULL_TERM_DAYS = 280

# Haemoglobin (g/dL) cut-offs used for anaemia in pregnancy (WHO / MoHFW)
HB_NORMAL = 11.0
HB_MILD = 10.0
HB_MODERATE = 7.0

VALID_RISK = {"all", "high", "normal"}
VALID_CASE_STATUS = {"all", "active", "delivered", "inactive"}


def _csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


# ── Filter container ─────────────────────────────────────────────────────────

@dataclass
class PWFilters:
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    block_id: Optional[int] = None
    sub_centre_id: Optional[int] = None
    ward_id: Optional[int] = None
    risk: str = "all"
    age_bands: List[str] = field(default_factory=list)
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    trimesters: List[str] = field(default_factory=list)
    parity: List[str] = field(default_factory=list)
    blood_groups: List[str] = field(default_factory=list)
    registration_source: Optional[str] = None      # self | staff
    case_status: str = "all"
    risk_factors: List[str] = field(default_factory=list)
    anaemic: Optional[bool] = None
    has_anc: Optional[bool] = None
    approval: Optional[str] = None                  # approved | pending

    def describe(self) -> dict:
        """Echo of the filters that were actually applied (for the UI chips)."""
        out = {}
        for k, v in self.__dict__.items():
            if v in (None, "", [], "all"):
                continue
            out[k] = v.isoformat() if isinstance(v, date) else v
        return out


def make_pw_filters_dep(default_case_status: str = "all"):
    """FastAPI dependency factory – parses the common beneficiary filters."""

    def pw_filters_dep(
        start_date: Optional[date] = Query(None),
        end_date: Optional[date] = Query(None),
        block_id: Optional[int] = Query(None),
        sub_centre_id: Optional[int] = Query(None),
        ward_id: Optional[int] = Query(None),
        risk: str = Query("all", description="all | high | normal"),
        age_band: Optional[str] = Query(None, description="comma list: lt18,18_24,25_29,30_34,35_plus,unknown"),
        age_min: Optional[int] = Query(None, ge=0, le=100),
        age_max: Optional[int] = Query(None, ge=0, le=100),
        trimester: Optional[str] = Query(None, description="comma list of 1,2,3,unknown (implies active pregnancies)"),
        parity: Optional[str] = Query(None, description="comma list: primi,multi,grand,unknown"),
        blood_group: Optional[str] = Query(None, description="comma list e.g. O+,A-"),
        registration_source: Optional[str] = Query(None, description="self | staff"),
        case_status: str = Query(default_case_status, description="all | active | delivered | inactive"),
        risk_factor: Optional[str] = Query(None, description="comma list; matches any"),
        anaemic: Optional[bool] = Query(None, description="true = has a Hb below 11 g/dL on record"),
        has_anc: Optional[bool] = Query(None, description="true = at least one ANC visit; false = none"),
        approval: Optional[str] = Query(None, description="approved | pending"),
    ) -> PWFilters:
        """FastAPI dependency – parses the common beneficiary filters."""
        if start_date and end_date and start_date > end_date:
            start_date, end_date = end_date, start_date
        if age_min is not None and age_max is not None and age_min > age_max:
            age_min, age_max = age_max, age_min
        return PWFilters(
            start_date=start_date,
            end_date=end_date,
            block_id=block_id,
            sub_centre_id=sub_centre_id,
            ward_id=ward_id,
            risk=risk if risk in VALID_RISK else "all",
            age_bands=[b for b in _csv(age_band) if b in AGE_BAND_LABELS],
            age_min=age_min,
            age_max=age_max,
            trimesters=[t for t in _csv(trimester) if t in ("1", "2", "3", "unknown")],
            parity=[p for p in _csv(parity) if p in ("primi", "multi", "grand", "unknown")],
            blood_groups=_csv(blood_group),
            registration_source=registration_source if registration_source in ("self", "staff") else None,
            case_status=case_status if case_status in VALID_CASE_STATUS else "all",
            risk_factors=_csv(risk_factor),
            anaemic=anaemic,
            has_anc=has_anc,
            approval=approval if approval in ("approved", "pending") else None,
        )

    return pw_filters_dep


pw_filters_dep = make_pw_filters_dep()


# ── Period helpers ───────────────────────────────────────────────────────────

def resolve_period(
    start: Optional[date], end: Optional[date], default_days: int = 90
) -> Tuple[date, date, date, date]:
    """
    Returns (start, end, prev_start, prev_end).
    The previous period is the window of identical length that ends the day
    before `start`, used for "vs previous period" comparisons.
    """
    today = date.today()
    end = end or today          # future end dates are allowed (e.g. upcoming USG load)
    start = start or (end - timedelta(days=default_days - 1))
    if start > end:
        start = end
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    return start, end, prev_start, prev_end


def gestation_bounds(trimester: str, today: Optional[date] = None):
    """(lmp_lower_exclusive, lmp_upper_inclusive) for a trimester, in LMP dates."""
    today = today or date.today()
    if trimester == "1":
        return today - timedelta(days=T2_START_DAYS), today
    if trimester == "2":
        return today - timedelta(days=T3_START_DAYS), today - timedelta(days=T2_START_DAYS)
    if trimester == "3":
        return None, today - timedelta(days=T3_START_DAYS)
    raise ValueError(trimester)


def _lmp_in_range(lo: Optional[date], hi: Optional[date]):
    """
    LMP in (lo, hi].  When LMP is missing we fall back to EDD − 280 days, which
    is expressed as a shifted range on EDD so no date arithmetic hits the DB.
    """
    lmp, edd = PregnantWoman.lmp_date, PregnantWoman.edd_date
    lmp_c = [lmp.isnot(None)]
    edd_c = [lmp.is_(None), edd.isnot(None)]
    if lo is not None:
        lmp_c.append(lmp > lo)
        edd_c.append(edd > lo + timedelta(days=FULL_TERM_DAYS))
    if hi is not None:
        lmp_c.append(lmp <= hi)
        edd_c.append(edd <= hi + timedelta(days=FULL_TERM_DAYS))
    return or_(and_(*lmp_c), and_(*edd_c))


def trimester_condition(trimesters: List[str]):
    parts = []
    for t in trimesters:
        if t == "unknown":
            parts.append(and_(PregnantWoman.lmp_date.is_(None), PregnantWoman.edd_date.is_(None)))
        else:
            lo, hi = gestation_bounds(t)
            parts.append(_lmp_in_range(lo, hi))
    return or_(*parts) if parts else None


def gestational_weeks(lmp: Optional[date], edd: Optional[date], today: Optional[date] = None) -> Optional[int]:
    """Python-side twin of the SQL logic above, for row-level display."""
    today = today or date.today()
    ref = lmp or (edd - timedelta(days=FULL_TERM_DAYS) if edd else None)
    if not ref:
        return None
    return max((today - ref).days // 7, 0)


def trimester_of(weeks: Optional[int]) -> str:
    if weeks is None:
        return "unknown"
    if weeks < 14:
        return "1"
    if weeks < 28:
        return "2"
    return "3"


# ── Scope (who may see which women) ──────────────────────────────────────────

def scope_conditions(user: User) -> list:
    """
    Jurisdiction limit for the logged-in user.  Fails closed: an unknown role, or
    a user without a jurisdiction id, sees nothing.
    """
    role = user.role
    if role == "district" and user.district_id is not None:
        return [PregnantWoman.district_id == user.district_id]
    if role == "block" and user.block_id is not None:
        return [PregnantWoman.block_id == user.block_id]
    if role == "sub_centre" and user.sub_centre_id is not None:
        return [_sub_centre_match(user.sub_centre_id)]
    return [false()]


def report_scope_conditions(user: User) -> list:
    """
    Jurisdiction limit used by the Reports module.  Same as ``scope_conditions``
    but a PMSMA user (who owns a block's PMSMA sessions) may also see the women of
    their own block.  Still fails closed for every other role.
    """
    if user.role == "pmsma" and user.block_id is not None:
        return [PregnantWoman.block_id == user.block_id]
    return scope_conditions(user)


def sub_centre_match(sub_centre_id: int):
    """Public alias: women belonging to a sub-centre directly or through a mapped ward."""
    return _sub_centre_match(sub_centre_id)


def _sub_centre_match(sub_centre_id: int):
    mapped_wards = select(WardSubcentreMapping.ward_id).where(
        WardSubcentreMapping.sub_centre_id == sub_centre_id
    )
    return or_(
        PregnantWoman.sub_centre_id == sub_centre_id,
        PregnantWoman.ward_id.in_(mapped_wards),
    )


# ── Main builder ─────────────────────────────────────────────────────────────

def pw_conditions(f: PWFilters, user: Optional[User] = None, include_scope: bool = True) -> list:
    """
    Build the list of SQL conditions that define the *cohort* of women.
    (Date range is NOT part of the cohort – each report applies it to the event
    it measures: registration date, visit date, scan date, ...)
    """
    conds: list = []
    PW = PregnantWoman

    if include_scope and user is not None:
        conds += scope_conditions(user)

    # Geography
    if f.block_id:
        conds.append(PW.block_id == f.block_id)
    if f.sub_centre_id:
        conds.append(_sub_centre_match(f.sub_centre_id))
    if f.ward_id:
        conds.append(PW.ward_id == f.ward_id)

    # Risk
    if f.risk == "high":
        conds.append(PW.is_high_risk == True)  # noqa: E712
    elif f.risk == "normal":
        conds.append(or_(PW.is_high_risk == False, PW.is_high_risk.is_(None)))  # noqa: E712
    if f.risk_factors:
        conds.append(or_(*[PW.risk_factors.ilike(f"%{rf}%") for rf in f.risk_factors]))

    # Age
    age_parts = []
    for band in f.age_bands:
        if band == "unknown":
            age_parts.append(PW.age.is_(None))
        else:
            lo, hi = AGE_BANDS[band]
            c = [PW.age.isnot(None)]
            if lo is not None:
                c.append(PW.age >= lo)
            if hi is not None:
                c.append(PW.age <= hi)
            age_parts.append(and_(*c))
    if age_parts:
        conds.append(or_(*age_parts))
    if f.age_min is not None:
        conds.append(PW.age >= f.age_min)
    if f.age_max is not None:
        conds.append(PW.age <= f.age_max)

    # Parity (gravida = number of pregnancies including this one)
    par_parts = []
    for p in f.parity:
        if p == "primi":
            par_parts.append(PW.gravida == 1)
        elif p == "multi":
            par_parts.append(and_(PW.gravida >= 2, PW.gravida <= 3))
        elif p == "grand":
            par_parts.append(PW.gravida >= 4)
        elif p == "unknown":
            par_parts.append(PW.gravida.is_(None))
    if par_parts:
        conds.append(or_(*par_parts))

    if f.blood_groups:
        conds.append(PW.blood_group.in_(f.blood_groups))

    if f.registration_source == "self":
        conds.append(PW.is_self_registered == True)  # noqa: E712
    elif f.registration_source == "staff":
        conds.append(or_(PW.is_self_registered == False, PW.is_self_registered.is_(None)))  # noqa: E712

    if f.approval == "approved":
        conds.append(PW.registration_approved == True)  # noqa: E712
    elif f.approval == "pending":
        conds.append(or_(PW.registration_approved == False, PW.registration_approved.is_(None)))  # noqa: E712

    # Case status.  A trimester filter only makes sense for ongoing pregnancies.
    case_status = f.case_status
    if f.trimesters and case_status == "all":
        case_status = "active"
    if case_status == "active":
        conds.append(PW.is_active == True)  # noqa: E712
    elif case_status == "delivered":
        conds.append(and_(PW.pregnancy_outcome.isnot(None), PW.pregnancy_outcome != ""))
    elif case_status == "inactive":
        conds.append(and_(
            or_(PW.is_active == False, PW.is_active.is_(None)),  # noqa: E712
            or_(PW.pregnancy_outcome.is_(None), PW.pregnancy_outcome == ""),
        ))

    tri = trimester_condition(f.trimesters)
    if tri is not None:
        conds.append(tri)

    # Care-state filters (EXISTS subqueries on ANC visits).  An alias is used so
    # the subquery never auto-correlates with an ANCVisit in the outer query.
    if f.anaemic is not None:
        a = aliased(ANCVisit)
        low_hb = exists().where(and_(
            a.pregnant_woman_id == PW.id,
            a.hemoglobin.isnot(None),
            a.hemoglobin < HB_NORMAL,
        ))
        conds.append(low_hb if f.anaemic else ~low_hb)
    if f.has_anc is not None:
        a2 = aliased(ANCVisit)
        any_anc = exists().where(a2.pregnant_woman_id == PW.id)
        conds.append(any_anc if f.has_anc else ~any_anc)

    return conds


def has_any_filter(f: PWFilters) -> bool:
    d = f.describe()
    d.pop("start_date", None)
    d.pop("end_date", None)
    return bool(d)
