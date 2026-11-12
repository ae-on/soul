-- =============================================================================
-- Миграция 000: таблица учёта применённых миграций
-- =============================================================================
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(255) NOT NULL PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);