-- Migration: Create ecg_reports table
-- Run this on existing deployments that don't use init_db auto-create

CREATE TABLE IF NOT EXISTS ecg_reports (
    id INT AUTO_INCREMENT PRIMARY KEY,
    pregnant_woman_id INT NOT NULL,
    dp_id INT NOT NULL,
    recorded_by_user_id INT NOT NULL,
    ecg_date DATE NOT NULL,
    result ENUM('normal', 'abnormal') NOT NULL,
    notes TEXT,
    report_file_path VARCHAR(500),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX ix_ecg_reports_pregnant_woman_id (pregnant_woman_id),
    INDEX ix_ecg_reports_dp_id (dp_id),
    INDEX ix_ecg_reports_result (result),
    INDEX ix_ecg_reports_ecg_date (ecg_date),

    CONSTRAINT fk_ecg_pregnant_woman FOREIGN KEY (pregnant_woman_id) REFERENCES pregnant_women(id),
    CONSTRAINT fk_ecg_dp FOREIGN KEY (dp_id) REFERENCES delivery_points(id),
    CONSTRAINT fk_ecg_recorded_by FOREIGN KEY (recorded_by_user_id) REFERENCES users(id)
);
