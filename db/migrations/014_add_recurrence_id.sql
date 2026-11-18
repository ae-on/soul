-- =============================================================================
-- Миграция 014: идентификатор набора повторений (recurrence.set_id из EM API).
-- Позволяет различать классы с одинаковым названием/временем/преподавателем,
-- но разными днями недели (например, «Трайбл» Вт/Чт и «Трайбл» Сб).
-- =============================================================================

ALTER TABLE events_cache
    ADD COLUMN recurrence_id INT UNSIGNED DEFAULT NULL COMMENT 'ID набора повторений (recurrence.set_id или event_id)';

ALTER TABLE slots
    ADD COLUMN recurrence_id INT UNSIGNED DEFAULT NULL COMMENT 'ID набора повторений из events_cache';
