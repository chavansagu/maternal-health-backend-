-- Migration: Add baby_count to delivery_outcomes + create delivery_outcome_babies table
-- Run once. DO NOT drop baby_gender (backward compat).

-- Step 1: Add baby_count column
ALTER TABLE delivery_outcomes
    ADD COLUMN IF NOT EXISTS baby_count INT NOT NULL DEFAULT 0;

-- Step 2: Create delivery_outcome_babies table
CREATE TABLE IF NOT EXISTS delivery_outcome_babies (
    id          SERIAL PRIMARY KEY,
    outcome_id  INT NOT NULL REFERENCES delivery_outcomes(id) ON DELETE CASCADE,
    baby_number INT NOT NULL,
    gender      VARCHAR(10) NOT NULL,
    status      VARCHAR(20) NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_outcome_baby_number UNIQUE (outcome_id, baby_number)
);

CREATE INDEX IF NOT EXISTS ix_delivery_outcome_babies_outcome_id ON delivery_outcome_babies(outcome_id);
CREATE INDEX IF NOT EXISTS ix_delivery_outcome_babies_status     ON delivery_outcome_babies(status);

-- Step 3: Backfill old records that have baby_gender but no babies rows yet
INSERT INTO delivery_outcome_babies (outcome_id, baby_number, gender, status, created_at)
SELECT
    id,
    1,
    baby_gender,
    'live_birth',
    created_at
FROM delivery_outcomes
WHERE baby_gender IS NOT NULL
  AND id NOT IN (SELECT DISTINCT outcome_id FROM delivery_outcome_babies);

-- Step 4: Update baby_count for backfilled records
UPDATE delivery_outcomes do_
SET    baby_count = sub.cnt
FROM  (
    SELECT outcome_id, COUNT(*) AS cnt
    FROM   delivery_outcome_babies
    GROUP  BY outcome_id
) sub
WHERE do_.id = sub.outcome_id
  AND do_.baby_count = 0;
