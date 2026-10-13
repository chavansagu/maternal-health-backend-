"""
One-time backfill: purani HRP women ke risk_factors text se hrp_conditions rows banata hai.

Run (backend folder se):
    python utilities/backfill_hrp_conditions.py --dry-run     # sirf dikhata hai, kuch save nahi
    python utilities/backfill_hrp_conditions.py               # asli run

Safe: jis woman ki hrp_conditions pehle se hain use skip karta hai (dobara chalane par duplicate nahi banenge).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal
from models import HRPCondition, PregnantWoman
from services.hrp_service import add_conditions, recompute_status

PREFIX_TO_SOURCE = {"pmsma": "pmsma", "usg": "usg", "auto-detected": "anc_auto", "anc": "anc_auto"}


def parse_risk_factors(text):
    """'PMSMA: PIH, GDM; USG: X; free text' -> [(source, name), ...]"""
    out = []
    for part in (text or "").split(";"):
        part = part.strip()
        if not part or part.lower() == "none":
            continue
        head, sep, tail = part.partition(":")
        source = PREFIX_TO_SOURCE.get(head.strip().lower()) if sep else None
        if source:
            for name in tail.split(","):
                name = name.strip()
                if name:
                    out.append((source, name[:150]))
        else:
            out.append(("manual", part[:150]))
    return out


def main(dry_run: bool):
    db = SessionLocal()
    done = skipped = 0
    try:
        pws = db.query(PregnantWoman).filter(PregnantWoman.is_high_risk == True).all()  # noqa: E712
        for pw in pws:
            if db.query(HRPCondition.id).filter(HRPCondition.pregnant_woman_id == pw.id).first():
                skipped += 1
                continue

            pairs = parse_risk_factors(pw.risk_factors) or [("manual", "Marked high risk (legacy)")]
            original_text = pw.risk_factors          # add_condition text badal deta hai, baad me wapas rakhenge

            for source in dict.fromkeys(s for s, _ in pairs):
                names = [n for s, n in pairs if s == source]
                add_conditions(db, pw, names, source, user_id=None,
                               reference_type="legacy_backfill")
            pw.risk_factors = original_text
            recompute_status(db, pw, note="Legacy backfill")

            print(f"PW {pw.id} {pw.full_name}: {len(pairs)} condition(s) -> {pw.hrp_status}")
            done += 1

        if dry_run:
            db.rollback()
            print(f"\nDRY RUN: {done} women process hui, {skipped} skip. Kuch save nahi hua.")
        else:
            db.commit()
            print(f"\nDONE: {done} women backfill hui, {skipped} skip.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main("--dry-run" in sys.argv)