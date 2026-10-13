"""
HRP module (spec D) - list + summary for Sub-centre, Block and District users.
Scope wahi hai jo baaki analytics mein use hota hai (analytics_filters.scope_conditions).
"""
from typing import List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from analytics_filters import scope_conditions
from auth import get_current_active_user
from database import get_db
from models import Block, HRPCondition, HRPStatusLog, PregnantWoman, SubCentre, User
from services.alert_rules import raise_alert
from services.hrp_service import (
    HRP_SOURCES, HRP_STATUSES, MANUAL_STATUSES,
    add_conditions, resolve_condition, set_manual_status,
)

router = APIRouter(prefix="/hrp", tags=["HRP"])

ALLOWED_ROLES = ("sub_centre", "block", "district")

STATUS_LABELS = {
    "no_risk": "No risk identified",
    "risk_identified": "Risk identified",
    "under_follow_up": "Under follow-up",
    "multiple_conditions": "Multiple HRP conditions",
    "delivery_planned": "Delivery planned",
    "case_closed": "Case closed / delivered",
}


def _check_role(user: User):
    if user.role not in ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="HRP list is available to sub-centre, block and district users only")


def _hrp_cohort(user, *, status=None, source=None, block_id=None, search=None, include_closed=False):
    """SQL conditions: user ke area ki HRP women."""
    PW = PregnantWoman
    conds = list(scope_conditions(user))
    conds.append(or_(PW.is_high_risk == True, PW.hrp_status != "no_risk"))  # noqa: E712

    if status:
        conds.append(PW.hrp_status == status)
    elif not include_closed:
        conds.append(PW.hrp_status != "case_closed")

    if source:
        conds.append(PW.id.in_(
            select(HRPCondition.pregnant_woman_id).where(
                HRPCondition.source == source, HRPCondition.is_active == True)  # noqa: E712
        ))
    if block_id:
        conds.append(PW.block_id == block_id)
    if search and search.strip():
        like = f"%{search.strip()}%"
        conds.append(or_(PW.full_name.ilike(like), PW.mobile_number.ilike(like),
                         PW.abha_id.ilike(like), PW.rch_id.ilike(like)))
    return conds


@router.get("/summary")
async def hrp_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Status-wise counts (dashboard cards)."""
    _check_role(current_user)
    conds = _hrp_cohort(current_user, include_closed=True)
    rows = (db.query(PregnantWoman.hrp_status, func.count(PregnantWoman.id))
              .filter(*conds).group_by(PregnantWoman.hrp_status).all())
    counts = {s: 0 for s in HRP_STATUSES}
    for st, n in rows:
        counts[st or "no_risk"] = n
    return {
        "total": sum(counts.values()),
        "by_status": [{"status": s, "label": STATUS_LABELS[s], "count": counts[s]} for s in HRP_STATUSES],
    }


@router.get("")
async def list_hrp(
    status: Optional[str] = Query(None, description="|".join(HRP_STATUSES)),
    source: Optional[str] = Query(None, description="|".join(HRP_SOURCES.keys())),
    block_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    include_closed: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _check_role(current_user)
    if status and status not in HRP_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {', '.join(HRP_STATUSES)}")
    if source and source not in HRP_SOURCES:
        raise HTTPException(status_code=400, detail=f"source must be one of {', '.join(HRP_SOURCES)}")

    PW = PregnantWoman
    conds = _hrp_cohort(current_user, status=status, source=source, block_id=block_id,
                        search=search, include_closed=include_closed)

    total = db.query(func.count(PW.id)).filter(*conds).scalar() or 0
    pws = (db.query(PW).filter(*conds)
             .order_by(PW.updated_at.desc(), PW.id.desc())
             .offset((page - 1) * per_page).limit(per_page).all())

    # In sab women ki active conditions ek hi query me
    ids = [p.id for p in pws]
    cond_map = {}
    if ids:
        for c in (db.query(HRPCondition)
                    .filter(HRPCondition.pregnant_woman_id.in_(ids), HRPCondition.is_active == True)  # noqa: E712
                    .order_by(HRPCondition.identified_at.asc()).all()):
            cond_map.setdefault(c.pregnant_woman_id, []).append({
                "id": c.id,
                "condition_name": c.condition_name,
                "source": c.source,
                "source_label": HRP_SOURCES.get(c.source, c.source),
                "identified_at": c.identified_at,
            })

    block_names = {b.id: b.name for b in db.query(Block.id, Block.name).all()}
    sc_names = {s.id: s.name for s in db.query(SubCentre.id, SubCentre.name).all()}

    items = []
    for p in pws:
        conditions = cond_map.get(p.id, [])
        items.append({
            "id": p.id,
            "full_name": p.full_name,
            "mobile_number": p.mobile_number,
            "age": p.age,
            "edd_date": p.edd_date,
            "block_id": p.block_id,
            "block_name": block_names.get(p.block_id),
            "sub_centre_id": p.sub_centre_id,
            "sub_centre_name": sc_names.get(p.sub_centre_id),
            "hrp_status": p.hrp_status,
            "hrp_status_label": STATUS_LABELS.get(p.hrp_status, p.hrp_status),
            "is_difficult_area": bool(p.is_difficult_area),
            "is_migrant_worker": bool(p.is_migrant_worker),
            "condition_count": len(conditions),
            "conditions": conditions,
            "risk_factors": p.risk_factors,
        })

    return {"total": total, "page": page, "per_page": per_page, "items": items}

# ─── Detail + history ────────────────────────────────────────────────────────

def _get_pw_in_scope(db: Session, user: User, pw_id: int) -> PregnantWoman:
    """User ke area ki woman, warna 404 (fail closed)."""
    pw = db.query(PregnantWoman).filter(PregnantWoman.id == pw_id, *scope_conditions(user)).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Pregnant woman not found")
    return pw


@router.get("/{pw_id}")
async def hrp_detail(
    pw_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Ek woman: saari conditions (source ke saath) + status change history."""
    _check_role(current_user)
    pw = _get_pw_in_scope(db, current_user, pw_id)

    conditions = (db.query(HRPCondition).filter(HRPCondition.pregnant_woman_id == pw.id)
                    .order_by(HRPCondition.identified_at.desc()).all())
    logs = (db.query(HRPStatusLog).filter(HRPStatusLog.pregnant_woman_id == pw.id)
              .order_by(HRPStatusLog.changed_at.desc(), HRPStatusLog.id.desc()).all())

    user_ids = {c.identified_by for c in conditions} | {c.resolved_by for c in conditions} | {l.changed_by for l in logs}
    user_ids.discard(None)
    names = {}
    if user_ids:
        names = {u.id: u.full_name for u in db.query(User.id, User.full_name).filter(User.id.in_(user_ids)).all()}

    return {
        "pregnant_woman": {
            "id": pw.id, "full_name": pw.full_name, "mobile_number": pw.mobile_number,
            "age": pw.age, "edd_date": pw.edd_date, "is_active": pw.is_active,
            "is_difficult_area": bool(pw.is_difficult_area), "is_migrant_worker": bool(pw.is_migrant_worker),
        },
        "hrp_status": pw.hrp_status,
        "hrp_status_label": STATUS_LABELS.get(pw.hrp_status, pw.hrp_status),
        "conditions": [{
            "id": c.id, "condition_name": c.condition_name, "category": c.category,
            "source": c.source, "source_label": HRP_SOURCES.get(c.source, c.source),
            "source_reference_type": c.source_reference_type, "source_reference_id": c.source_reference_id,
            "is_active": c.is_active, "remarks": c.remarks,
            "identified_at": c.identified_at, "identified_by": names.get(c.identified_by, "System" if c.identified_by is None else None),
            "resolved_at": c.resolved_at, "resolved_by": names.get(c.resolved_by),
        } for c in conditions],
        "history": [{
            "id": l.id, "action": l.action, "old_status": l.old_status, "new_status": l.new_status,
            "source": l.source, "note": l.note, "changed_at": l.changed_at,
            "changed_by": names.get(l.changed_by, "System" if l.changed_by is None else None),
        } for l in logs],
    }


# ─── Manual status change ────────────────────────────────────────────────────

class HRPStatusChange(BaseModel):
    status: str                      # under_follow_up | delivery_planned | auto
    note: Optional[str] = None


@router.post("/{pw_id}/status")
async def change_hrp_status(
    pw_id: int,
    body: HRPStatusChange,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Under follow-up / Delivery planned set karo. 'auto' = manual status hatao, conditions se nikalo."""
    _check_role(current_user)
    if body.status != "auto" and body.status not in MANUAL_STATUSES:
        raise HTTPException(status_code=400, detail="status must be under_follow_up, delivery_planned or auto")

    pw = _get_pw_in_scope(db, current_user, pw_id)
    if pw.hrp_status == "case_closed":
        raise HTTPException(status_code=400, detail="HRP case is closed / delivered; status cannot be changed")
    if not pw.is_high_risk and (pw.hrp_status or "no_risk") == "no_risk":
        raise HTTPException(status_code=400, detail="No HRP condition on this woman")

    new_status = set_manual_status(db, pw, body.status, user_id=current_user.id, note=body.note)
    db.commit()
    return {"id": pw.id, "hrp_status": new_status, "hrp_status_label": STATUS_LABELS.get(new_status, new_status)}


# ─── Resolve a condition ─────────────────────────────────────────────────────

class HRPResolve(BaseModel):
    note: Optional[str] = None


@router.post("/conditions/{condition_id}/resolve")
async def resolve_hrp_condition(
    condition_id: int,
    body: HRPResolve,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Condition band karo (delete nahi hoti, history me rehti hai)."""
    _check_role(current_user)
    cond = db.query(HRPCondition).filter(HRPCondition.id == condition_id).first()
    if not cond:
        raise HTTPException(status_code=404, detail="Condition not found")
    pw = _get_pw_in_scope(db, current_user, cond.pregnant_woman_id)
    if not cond.is_active:
        raise HTTPException(status_code=400, detail="Condition already resolved")

    resolve_condition(db, pw, cond, user_id=current_user.id, note=body.note)
    db.commit()
    return {"id": cond.id, "is_active": cond.is_active, "hrp_status": pw.hrp_status}


# ─── Add condition manually ──────────────────────────────────────────────────

class HRPConditionAdd(BaseModel):
    conditions: List[str]            # ek ya zyada condition names
    source: str = "mcp_card"         # mcp_card | manual
    remarks: Optional[str] = None


@router.post("/{pw_id}/conditions")
async def add_hrp_conditions(
    pw_id: int,
    body: HRPConditionAdd,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Woman me HRP condition(s) add karo (purani conditions replace nahi hoti)."""
    _check_role(current_user)
    if body.source not in ("mcp_card", "manual"):
        raise HTTPException(status_code=400, detail="source must be mcp_card or manual")

    names = [n.strip()[:150] for n in body.conditions if n and n.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="At least one condition is required")
    if len(names) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 conditions at a time")

    pw = _get_pw_in_scope(db, current_user, pw_id)
    if pw.hrp_status == "case_closed" or not pw.is_active:
        raise HTTPException(status_code=400, detail="Cannot add conditions to a closed / delivered case")

    res = add_conditions(db, pw, names, body.source, user_id=current_user.id,
                         reference_type="manual_entry", remarks=body.remarks)
    db.commit()

    if res["newly_flagged"]:
        raise_alert(
            db, "hrp_identified", pw,
            title="⚠️ High-Risk Case Identified",
            message=f"{pw.full_name} has been tagged high-risk ({', '.join(names)}).",
            alert_key=f"hrp_identified:{pw.id}",
            reference_id=pw.id,
            reference_type="pregnant_woman",
            action_url=f"/pregnant-women/{pw.id}",
            metadata={"pregnant_woman_name": pw.full_name, "risk_factors": pw.risk_factors},
        )

    return {
        "id": pw.id,
        "added": [c.condition_name for c in res["created"]],
        "skipped_already_active": len(names) - len(res["created"]),
        "hrp_status": pw.hrp_status,
        "hrp_status_label": STATUS_LABELS.get(pw.hrp_status, pw.hrp_status),
    }