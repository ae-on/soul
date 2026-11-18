-- =============================================================================
-- Миграция 013: Recurring Events — событие может повторяться по датам.
-- Уникальный ключ events_cache меняется с em_event_id на (em_event_id, start_date),
-- чтобы одно событие могло хранить несколько дат (по одной записи на дату).
-- =============================================================================

ALTER TABLE events_cache
    DROP INDEX idx_em_event_id,
    ADD UNIQUE INDEX idx_em_event_date (em_event_id, start_date);