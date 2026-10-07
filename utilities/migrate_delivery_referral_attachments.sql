-- Migration: Add treatment_given and attachment columns to delivery_referrals
-- Date: 2025
-- Safe to run: all columns are nullable, existing data is unaffected

ALTER TABLE delivery_referrals
    ADD COLUMN treatment_given TEXT NULL AFTER re_refer_reason,
    ADD COLUMN re_refer_attachment_path VARCHAR(500) NULL AFTER treatment_given,
    ADD COLUMN re_refer_attachment_paths TEXT NULL AFTER re_refer_attachment_path;
