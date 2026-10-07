"""
Migration: Add 'DP' to users.role ENUM column
Run once: python utilities/migrate_role_enum.py
"""
from database import engine
from sqlalchemy import text

with engine.connect() as conn:
    conn.execute(text(
        "ALTER TABLE users MODIFY COLUMN role ENUM('district','block','sub_centre','usg_centre','dp','pmsma') NOT NULL"
    ))
    conn.commit()
    print("✅ Migration complete: 'pmsma' added to users.role ENUM")
