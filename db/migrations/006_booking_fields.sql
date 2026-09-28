-- =============================================================================
-- Миграция 006: расширение полей bookings, статусы, event_date/event_time
-- =============================================================================

-- 1. Добавляем event_date, event_time, refunded
ALTER TABLE bookings
    ADD COLUMN event_date DATE DEFAULT NULL COMMENT 'Дата занятия из EM',
    ADD COLUMN event_time TIME DEFAULT NULL COMMENT 'Время начала занятия из EM',
    ADD COLUMN refunded TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'Были ли возвращены часы при отмене';

-- 2. Расширяем ENUM статусов
ALTER TABLE bookings
    MODIFY COLUMN status ENUM(
        'registered',
        'pending',
        'confirmed',
        'cancelled',
        'late_cancel',
        'attended',
        'no_show',
        'skipped'
    ) NOT NULL DEFAULT 'registered';
