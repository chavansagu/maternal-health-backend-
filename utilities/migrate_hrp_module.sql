ALTER TABLE pregnant_women
  ADD COLUMN IF NOT EXISTS hrp_status VARCHAR(30) NOT NULL DEFAULT 'no_risk',
  ADD COLUMN IF NOT EXISTS is_difficult_area TINYINT(1) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS is_migrant_worker TINYINT(1) NOT NULL DEFAULT 0;

CREATE INDEX idx_pw_hrp_status ON pregnant_women (hrp_status);

-- Purane data ka backfill: jo already high-risk hain unka status set karo
UPDATE pregnant_women SET hrp_status = 'risk_identified'
 WHERE is_high_risk = 1 AND hrp_case_status IS NULL;
UPDATE pregnant_women SET hrp_status = 'case_closed'
 WHERE hrp_case_status IN ('delivered', 'case_closed');