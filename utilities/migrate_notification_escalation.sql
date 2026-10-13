-- Notification & Escalation module (spec M) - schema changes
-- Safe to run more than once (MariaDB 10.0.2+ supports ADD COLUMN / INDEX IF NOT EXISTS).
-- On plain MySQL (no IF NOT EXISTS) remove the clause and skip columns that already exist.
--
-- NOTE: new tables (escalation_logs, device_tokens) are created automatically at app startup
-- by init_db() -> Base.metadata.create_all(); only changes to the EXISTING notifications table
-- need this script.

-- 1) Alert tracking columns (the 2026-06-25 backup predates these; skip if already present)
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS alert_key        VARCHAR(100) NULL;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS is_resolved      TINYINT(1)   NOT NULL DEFAULT 0;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS resolved_at      DATETIME     NULL;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS escalation_level VARCHAR(20)  NULL;

-- 2) Offline sync acknowledgement
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS delivered_at     DATETIME     NULL;

-- 3) Indexes used by dedupe, closure, sync and the escalation job
CREATE INDEX IF NOT EXISTS ix_notifications_alert_key    ON notifications (alert_key);
CREATE INDEX IF NOT EXISTS ix_notifications_is_resolved  ON notifications (is_resolved);
CREATE INDEX IF NOT EXISTS ix_notifications_delivered_at ON notifications (delivered_at);
