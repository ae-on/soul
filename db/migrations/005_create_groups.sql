-- =============================================================================
-- Миграция 005: таблица groups (слоты / классы)
-- =============================================================================

CREATE TABLE IF NOT EXISTS slots (
    id              INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name            VARCHAR(150) NOT NULL COMMENT 'Название слота: Бачата Пн/Ср 18:00',
    category_slug   VARCHAR(50) NOT NULL COMMENT 'bachata, yoga, tribal, ...',
    weekdays        VARCHAR(20) NOT NULL COMMENT 'Дни недели: 1,3 (1=Пн, 7=Вс)',
    time_start      TIME NOT NULL,
    time_end        TIME NOT NULL,
    duration_hours  DECIMAL(4,1) NOT NULL COMMENT 'Вычисляется: 1.0 или 1.5',
    teacher_id      INT UNSIGNED DEFAULT NULL COMMENT 'FK users.id, роль teacher',
    location_id     INT UNSIGNED DEFAULT NULL COMMENT 'ID зала (1, 2, ...)',
    max_spots       INT UNSIGNED NOT NULL DEFAULT 15,
    description     TEXT DEFAULT NULL,
    active          TINYINT(1) NOT NULL DEFAULT 1,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    KEY idx_category (category_slug),
    KEY idx_teacher (teacher_id),
    KEY idx_active (active),

    CONSTRAINT fk_groups_teacher
        FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
