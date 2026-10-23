-- =============================================================================
-- Миграция 009: добавляем teacher_id в events_cache для привязки событий
-- к преподавателям (из EM owner или из слотов по категории+времени)
-- =============================================================================

ALTER TABLE events_cache
    ADD COLUMN teacher_id INT UNSIGNED DEFAULT NULL COMMENT 'Кто ведёт (users.id, из EM owner или слотов)',
    ADD INDEX idx_teacher_id (teacher_id);