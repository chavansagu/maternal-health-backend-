-- Migration: Create pmsma_centres table (MySQL)
CREATE TABLE IF NOT EXISTS pmsma_centres (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    code VARCHAR(50) UNIQUE NOT NULL,
    address TEXT,
    contact_number VARCHAR(15),
    contact_person_name VARCHAR(255),
    district_id INT NOT NULL,
    block_id INT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at DATETIME DEFAULT NOW(),
    updated_at DATETIME DEFAULT NOW() ON UPDATE NOW(),
    FOREIGN KEY (district_id) REFERENCES districts(id),
    FOREIGN KEY (block_id) REFERENCES blocks(id)
);
