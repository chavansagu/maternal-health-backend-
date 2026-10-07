-- Migration: Add job_status column to ai_query_history
-- Run once on production DB

ALTER TABLE ai_query_history
ADD COLUMN job_status VARCHAR(20) NOT NULL DEFAULT 'completed'
AFTER visualization_config;

-- Mark all existing records as completed (they were synchronous, already have results)
UPDATE ai_query_history SET job_status = 'completed' WHERE success = 1;
UPDATE ai_query_history SET job_status = 'failed' WHERE success = 0;
