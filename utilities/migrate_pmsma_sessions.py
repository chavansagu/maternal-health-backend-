"""
Migration: Create pmsma_sessions table and add is_sdh_dhh to delivery_points.
Run once from the janani-jyoti_v2.1 directory:
    python utilities/migrate_pmsma_sessions.py
"""
from database import engine
from sqlalchemy import text

DDL = [
    # 1. Add is_sdh_dhh to delivery_points (idempotent via IF NOT EXISTS workaround)
    """
    ALTER TABLE delivery_points
    ADD COLUMN IF NOT EXISTS is_sdh_dhh TINYINT(1) NOT NULL DEFAULT 0
    """,

    # 2. Create pmsma_sessions table
    """
    CREATE TABLE IF NOT EXISTS pmsma_sessions (
        id                    INT AUTO_INCREMENT PRIMARY KEY,
        pregnant_woman_id     INT NOT NULL,
        scheduled_date        DATETIME NOT NULL,
        original_scheduled_date DATETIME,
        block_id              INT,
        site                  VARCHAR(255),
        status                VARCHAR(20) NOT NULL DEFAULT 'scheduled',
        appointment_type      VARCHAR(20) NOT NULL DEFAULT 'regular',
        bp                    VARCHAR(20),
        blood_sugar           FLOAT,
        hb                    FLOAT,
        weight                FLOAT,
        additional_parameters TEXT,
        counselling_notes     TEXT,
        is_high_risk          TINYINT(1) NOT NULL DEFAULT 0,
        scheduled_by          INT NOT NULL,
        completed_by          INT,
        reschedule_reason     TEXT,
        is_emergency_override TINYINT(1) NOT NULL DEFAULT 0,
        override_reason       TEXT,
        created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at            DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX ix_pmsma_sessions_pregnant_woman_id (pregnant_woman_id),
        CONSTRAINT fk_pmsma_pw   FOREIGN KEY (pregnant_woman_id) REFERENCES pregnant_women(id),
        CONSTRAINT fk_pmsma_block FOREIGN KEY (block_id)          REFERENCES blocks(id),
        CONSTRAINT fk_pmsma_sched FOREIGN KEY (scheduled_by)      REFERENCES users(id),
        CONSTRAINT fk_pmsma_comp  FOREIGN KEY (completed_by)      REFERENCES users(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]

with engine.connect() as conn:
    for stmt in DDL:
        conn.execute(text(stmt.strip()))
    conn.commit()
    print("✅ Migration complete: pmsma_sessions table created, is_sdh_dhh added to delivery_points")
