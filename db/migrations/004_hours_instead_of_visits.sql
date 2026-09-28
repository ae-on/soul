-- =============================================================================
-- Миграция 004: замена учёта «занятий» на «часы»
-- =============================================================================

-- 1. subscription_types: visits_count → hours_count
ALTER TABLE subscription_types
    DROP COLUMN visits_count,
    ADD COLUMN hours_count DECIMAL(4,1) DEFAULT NULL
        COMMENT 'Часов в абонементе, NULL для безлимита';

-- 2. subscriptions: visits_total/visits_left → hours_total/hours_left
ALTER TABLE subscriptions
    DROP COLUMN visits_total,
    DROP COLUMN visits_left,
    ADD COLUMN hours_total DECIMAL(4,1) DEFAULT NULL
        COMMENT 'Всего часов в абонементе, NULL для безлимита',
    ADD COLUMN hours_left DECIMAL(4,1) DEFAULT NULL
        COMMENT 'Остаток часов, NULL для безлимита';

-- 3. bookings: добавить hours_charged
ALTER TABLE bookings
    ADD COLUMN hours_charged DECIMAL(4,1) DEFAULT NULL
        COMMENT 'Сколько часов списывается (1.0 или 1.5)';

-- 4. visits: добавить hours_charged
ALTER TABLE visits
    ADD COLUMN hours_charged DECIMAL(4,1) DEFAULT NULL
        COMMENT 'Сколько часов списано фактически';
