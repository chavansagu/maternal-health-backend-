-- Migration: Seed global "Other DP Point"
-- This is a special delivery point that cannot be deleted.
-- It is used when the actual delivery facility is unknown or not in the system.
-- Sub-Centre users can record delivery outcomes for referrals sent to this DP.

INSERT INTO delivery_points (name, code, address, contact_number, contact_person_name, district_id, block_id, is_active, created_at, updated_at)
SELECT 'Other DP Point', 'OTHER_DP_GLOBAL', 'N/A', NULL, NULL, 1, NULL, 1, NOW(), NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM delivery_points WHERE code = 'OTHER_DP_GLOBAL'
);
