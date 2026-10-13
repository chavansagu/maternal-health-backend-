"""
HRP service (spec D)
Ek hi jagah: condition add karna (source ke saath), status nikalna, change log likhna.
Functions commit nahi karte; commit caller karega (SessionLocal autoflush=False hai, isliye andar flush() use hua hai).
"""
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from models import HRPCondition, HRPStatusLog

HRP_SOURCES = {
    "mcp_card": "MCP Card",
    "difficult_area": "Difficult Area",
    "migrant_worker": "Migrant Worker",
    "pmsma": "PMSMA",
    "usg": "USG",
    "anc_auto": "ANC",
    "manual": "Manual",
}

HRP_STATUSES = [
    "no_risk", "risk_identified", "under_follow_up",
    "multiple_conditions", "delivery_planned", "case_closed",
]
# Ye 2 status sirf user (Step 4 ke route se) set karta hai; auto-calc inhe nahi badalta
MANUAL_STATUSES = {"under_follow_up", "delivery_planned"}


def log_hrp(db: Session, pw_id: int, action: str, *, old_status=None, new_status=None,
            condition=None, source=None, note=None, user_id: Optional[int] = None) -> None:
    db.add(HRPStatusLog(
        pregnant_woman_id=pw_id, action=action,
        old_status=old_status, new_status=new_status,
        condition_id=condition.id if condition is not None else None,
        source=source, note=note, changed_by=user_id, changed_at=datetime.now(),
    ))


def recompute_status(db: Session, pw, user_id: Optional[int] = None, note: Optional[str] = None) -> str:
    """Active conditions ke hisaab se status set karta hai; badla to log likhta hai."""
    db.flush()
    old = pw.hrp_status or "no_risk"

    if pw.hrp_case_status in ("delivered", "case_closed"):
        new = "case_closed"
    elif old in MANUAL_STATUSES:
        new = old
    else:
        n = (db.query(HRPCondition.condition_name)
               .filter(HRPCondition.pregnant_woman_id == pw.id, HRPCondition.is_active == True)  # noqa: E712
               .distinct().count())
        new = "no_risk" if n == 0 else ("risk_identified" if n == 1 else "multiple_conditions")

    if new != old:
        pw.hrp_status = new
        log_hrp(db, pw.id, "status_changed", old_status=old, new_status=new,
                note=note or "Auto status update", user_id=user_id)
    return new


def add_condition(db: Session, pw, condition_name: str, source: str, *,
                  user_id: Optional[int] = None, reference_type: Optional[str] = None,
                  reference_id: Optional[int] = None, remarks: Optional[str] = None):
    """Ek condition add (ya band wali dobara active). Naya row; purane replace nahi hote.
    Return: HRPCondition agar kuch naya hua, warna None (already active tha)."""
    name = (condition_name or "").strip()
    if not name or source not in HRP_SOURCES:
        return None

    row = db.query(HRPCondition).filter(
        HRPCondition.pregnant_woman_id == pw.id,
        HRPCondition.condition_name == name,
        HRPCondition.source == source,
    ).first()

    if row:
        if row.is_active:
            return None
        row.is_active = True
        row.resolved_at = None
        row.resolved_by = None
        row.identified_at = datetime.now()
        row.identified_by = user_id
    else:
        row = HRPCondition(
            pregnant_woman_id=pw.id, condition_name=name, source=source,
            source_reference_type=reference_type, source_reference_id=reference_id,
            remarks=remarks, identified_by=user_id, identified_at=datetime.now(),
        )
        db.add(row)
    db.flush()

    log_hrp(db, pw.id, "condition_added", condition=row, source=source,
            note=f"{HRP_SOURCES[source]}: {name}", user_id=user_id)

    # Purane code/reports ke liye risk_factors text bhi sync rakho
    if source != "manual":
        text = f"{HRP_SOURCES[source]}: {name}"
        existing = (pw.risk_factors or "").strip()
        if existing.lower() == "none":
            existing = ""
        if text not in existing:
            pw.risk_factors = (existing + "; " if existing else "") + text
    pw.is_high_risk = True
    return row


def add_conditions(db: Session, pw, names: Iterable[str], source: str, *,
                   user_id: Optional[int] = None, reference_type: Optional[str] = None,
                   reference_id: Optional[int] = None, remarks: Optional[str] = None) -> dict:
    """Kai conditions ek saath, phir status ek baar recompute.
    Return: {"created": [HRPCondition...], "newly_flagged": bool}  (newly_flagged = pehli baar HRP bana)"""
    was_hrp = bool(pw.is_high_risk)
    created = []
    for n in names:
        row = add_condition(db, pw, n, source, user_id=user_id, reference_type=reference_type,
                            reference_id=reference_id, remarks=remarks)
        if row:
            created.append(row)
    if created:
        recompute_status(db, pw, user_id=user_id, note=f"Condition(s) added from {HRP_SOURCES.get(source, source)}")
    return {"created": created, "newly_flagged": bool(created) and not was_hrp}

def set_manual_status(db: Session, pw, status: str, user_id: Optional[int] = None,
                      note: Optional[str] = None) -> str:
    """under_follow_up / delivery_planned set karo. status='auto' se manual lock hatta ke
    conditions ke hisaab se status wapas nikalo."""
    old = pw.hrp_status or "no_risk"
    if status == "auto":
        n = (db.query(HRPCondition.condition_name)
               .filter(HRPCondition.pregnant_woman_id == pw.id, HRPCondition.is_active == True)  # noqa: E712
               .distinct().count())
        new = "no_risk" if n == 0 else ("risk_identified" if n == 1 else "multiple_conditions")
    else:
        new = status
    if new != old:
        pw.hrp_status = new
        log_hrp(db, pw.id, "status_changed", old_status=old, new_status=new,
                note=note or "Status changed manually", user_id=user_id)
    return new


def resolve_condition(db: Session, pw, condition, user_id: Optional[int] = None,
                      note: Optional[str] = None) -> bool:
    """Condition band karo (history me rehti hai). Sab conditions band + status no_risk => is_high_risk False."""
    if not condition.is_active:
        return False
    condition.is_active = False
    condition.resolved_at = datetime.now()
    condition.resolved_by = user_id
    log_hrp(db, pw.id, "condition_resolved", condition=condition, source=condition.source,
            note=note or f"{condition.condition_name} resolved", user_id=user_id)
    recompute_status(db, pw, user_id=user_id, note="Condition resolved")
    if (pw.hrp_status or "no_risk") == "no_risk":
        pw.is_high_risk = False
    return True

def sync_flag_condition(db: Session, pw, source: str, name: str, is_on: bool,
                        user_id: Optional[int] = None) -> None:
    """Checkbox jaise flags (difficult_area / migrant_worker): ON => condition add,
    OFF => us source ki active conditions resolve."""
    if is_on:
        add_conditions(db, pw, [name], source, user_id=user_id)
        return
    rows = db.query(HRPCondition).filter(
        HRPCondition.pregnant_woman_id == pw.id,
        HRPCondition.source == source,
        HRPCondition.is_active == True,  # noqa: E712
    ).all()
    for r in rows:
        resolve_condition(db, pw, r, user_id=user_id, note=f"{name} flag removed")