-- =============================================================================
-- Миграция 012: telegram_id становится nullable
-- (админ может добавить пользователя без Telegram ID)
-- =============================================================================

ALTER TABLE users
    MODIFY COLUMN telegram_id BIGINT UNSIGNED DEFAULT NULL COMMENT 'Telegram ID (может отсутствовать)';