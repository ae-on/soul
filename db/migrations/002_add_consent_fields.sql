-- =============================================================================
-- Миграция 002: поля согласия на обработку ПДн
-- =============================================================================

ALTER TABLE users
    ADD COLUMN consent_given TINYINT(1) NOT NULL DEFAULT 0 AFTER last_visit_at,
    ADD COLUMN consent_date DATETIME DEFAULT NULL AFTER consent_given,
    ADD COLUMN consent_text_version VARCHAR(20) DEFAULT NULL AFTER consent_date;

ALTER TABLE users ADD INDEX idx_consent (consent_given);
