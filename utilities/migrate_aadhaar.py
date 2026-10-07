"""
Aadhaar Migration Script
========================
Migrates existing plain-text Aadhaar numbers in the DB to:
  - aadhaar_number  → SHA-256 hash (for fast, direct duplicate check)
  - aadhaar_masked  → XXXXXXXX9012 (for frontend display)

Usage:
    python utilities/migrate_aadhaar.py

Run AFTER executing these SQL statements:
    ALTER TABLE pregnant_women MODIFY aadhaar_number VARCHAR(255);
    ALTER TABLE pregnant_women ADD COLUMN aadhaar_masked VARCHAR(20) NULL;
"""

import sys
import os
import logging
from datetime import datetime

# Add project root to path so imports work
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal
from models import PregnantWoman
# Aadhaar migration disabled for current release
# from auth import hash_aadhaar, mask_aadhaar

# ── Logging setup ──────────────────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrate_aadhaar.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def is_already_hashed(value: str) -> bool:
    """Aadhaar migration helpers disabled for current release"""
    return False


def migrate():
    logger.info("Aadhaar migration is disabled in the current release. No action was taken.")
    return

    # ── Summary ────────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("Migration Summary")
    logger.info(f"  Total records found : {total}")
    logger.info(f"  Migrated            : {migrated}")
    logger.info(f"  Skipped             : {skipped}")
    logger.info(f"  Failed              : {failed}")
    logger.info(f"  Log file            : {LOG_FILE}")
    logger.info("=" * 60)

    if failed > 0:
        logger.warning(f"{failed} record(s) failed. Check log for details.")
    else:
        logger.info("Migration completed successfully with no failures.")


# if __name__ == "__main__":
#     migrate()
