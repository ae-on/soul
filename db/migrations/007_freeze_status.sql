-- =============================================================================
-- Миграция 007: freezes — статусы заявок и фактическое время заморозки
-- =============================================================================

-- Один ALTER TABLE — один statement (скрипт run_migration.py разбивает по `;`)

ALTER TABLE freezes
    ADD COLUMN status ENUM(
        'pending','approved','rejected','cancelled','completed'
    ) NOT NULL DEFAULT 'approved',
    ADD COLUMN reviewed_by INT UNSIGNED DEFAULT NULL,
    ADD COLUMN reviewed_at DATETIME DEFAULT NULL,
    ADD COLUMN actual_end_date DATE DEFAULT NULL,
    ADD COLUMN days_actual INT DEFAULT NULL,
    ADD INDEX idx_status (status);