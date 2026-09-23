-- =============================================================================
-- Миграция 003: добавление статуса deleted_data в users.status
-- =============================================================================

ALTER TABLE users
    MODIFY COLUMN status ENUM(
        'new','active','expired','archived','inactive','deleted_data'
    ) NOT NULL DEFAULT 'new';
