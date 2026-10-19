-- =============================================================================
-- Миграция 008: расширение enum'ов для админки абонементов
--   subscriptions.status — добавляем 'cancelled' (отмена абонемента админом)
--   visits.status        — добавляем 'attended' (ручная отметка посещения)
-- =============================================================================

ALTER TABLE subscriptions
    MODIFY COLUMN status ENUM('active','frozen','expired','depleted','cancelled')
        NOT NULL DEFAULT 'active';

ALTER TABLE visits
    MODIFY COLUMN status ENUM('present','absent','cancelled','attended')
        NOT NULL DEFAULT 'present';