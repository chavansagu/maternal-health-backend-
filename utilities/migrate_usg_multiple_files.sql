-- Migration: Add multiple file path columns to usg_appointments
-- Date: 2025
-- Safe to run: both columns are nullable, existing data is unaffected

ALTER TABLE usg_appointments
    ADD COLUMN prescription_file_paths TEXT NULL AFTER prescription_file_path,
    ADD COLUMN report_file_paths TEXT NULL AFTER report_file_path;
