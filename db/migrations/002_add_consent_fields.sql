-- =============================================================================
-- Миграция 002: поля согласия на обработку ПДн
-- =============================================================================

ALTER TABLE users
    ADD COLUMN consent_given TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN consent_date DATETIME DEFAULT NULL,
    ADD COLUMN consent_text_version VARCHAR(20) DEFAULT NULL;

ALTER TABLE users ADD INDEX idx_consent (consent_given);
