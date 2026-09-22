-- =============================================================================
-- Схема MySQL для CRM студии танцев и йоги SOUL
-- Движок: InnoDB, кодировка: utf8mb4_unicode_ci
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. users — все пользователи системы
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    telegram_id     BIGINT UNSIGNED NOT NULL,
    username        VARCHAR(255) DEFAULT NULL COMMENT 'Telegram @username',
    full_name       VARCHAR(255) NOT NULL,
    phone           VARCHAR(50) NOT NULL,
    email           VARCHAR(255) DEFAULT NULL,
    role            ENUM('student','teacher','admin','super_admin') NOT NULL DEFAULT 'student',
    status          ENUM('new','active','expired','archived','inactive') NOT NULL DEFAULT 'new',
    registered_at   DATETIME NOT NULL COMMENT 'Дата первой регистрации в боте',
    last_activity_at DATETIME DEFAULT NULL COMMENT 'Последняя активность в боте',
    last_visit_at   DATETIME DEFAULT NULL COMMENT 'Последнее посещение занятия',
    notes           TEXT DEFAULT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX idx_telegram_id (telegram_id),
    INDEX idx_role (role),
    INDEX idx_status (status),
    INDEX idx_last_visit (last_visit_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 2. subscription_types — справочник типов абонементов
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscription_types (
    id                INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name              VARCHAR(100) NOT NULL,
    visits_count      INT UNSIGNED DEFAULT NULL COMMENT 'NULL — безлимит',
    duration_days     INT UNSIGNED NOT NULL DEFAULT 30,
    price_byn         DECIMAL(8,2) NOT NULL,
    freeze_count_max  INT UNSIGNED NOT NULL DEFAULT 1,
    freeze_days_max   INT UNSIGNED NOT NULL DEFAULT 14,
    cross_group       TINYINT(1) NOT NULL DEFAULT 1 COMMENT 'Можно на другие направления',
    active            TINYINT(1) NOT NULL DEFAULT 1,
    sort_order        INT UNSIGNED NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 3. subscriptions — купленные абонементы
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscriptions (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id         INT UNSIGNED NOT NULL,
    type_id         INT UNSIGNED NOT NULL,
    purchased_at    DATETIME NOT NULL COMMENT 'Дата покупки',
    started_at      DATETIME NOT NULL COMMENT 'Дата начала действия',
    expires_at      DATETIME DEFAULT NULL COMMENT 'Срок окончания (сдвигается при заморозке)',
    visits_total    INT UNSIGNED DEFAULT NULL COMMENT 'NULL — безлимит',
    visits_left     INT UNSIGNED DEFAULT NULL,
    freeze_used     INT UNSIGNED NOT NULL DEFAULT 0,
    status          ENUM('active','frozen','expired','depleted') NOT NULL DEFAULT 'active',
    comment         TEXT DEFAULT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_user_id (user_id),
    INDEX idx_status (status),
    INDEX idx_expires_at (expires_at),

    CONSTRAINT fk_subscriptions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT fk_subscriptions_type FOREIGN KEY (type_id) REFERENCES subscription_types(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 4. freezes — история заморозок абонементов
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS freezes (
    id                INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    subscription_id   INT UNSIGNED NOT NULL,
    start_date        DATE NOT NULL,
    end_date          DATE NOT NULL,
    days              INT UNSIGNED GENERATED ALWAYS AS (DATEDIFF(end_date, start_date)) STORED,
    reason            VARCHAR(255) DEFAULT NULL COMMENT 'болезнь, отпуск, другое',
    created_by        INT UNSIGNED DEFAULT NULL COMMENT 'Кто оформил (users.id)',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_subscription_id (subscription_id),
    INDEX idx_start_date (start_date),
    INDEX idx_end_date (end_date),

    CONSTRAINT fk_freezes_subscription FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE RESTRICT,
    CONSTRAINT fk_freezes_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 5. events_cache — кэш событий из Events Manager
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events_cache (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    em_event_id     BIGINT UNSIGNED NOT NULL,
    name            VARCHAR(255) NOT NULL,
    start_date      DATE NOT NULL,
    start_time      TIME NOT NULL,
    end_time        TIME DEFAULT NULL,
    location_id     INT UNSIGNED DEFAULT NULL,
    location_name   VARCHAR(100) DEFAULT NULL,
    category_slug   VARCHAR(100) DEFAULT NULL,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX idx_em_event_id (em_event_id),
    INDEX idx_start_date (start_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 6. bookings — регистрации на занятие
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bookings (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id         INT UNSIGNED NOT NULL,
    em_event_id     BIGINT UNSIGNED NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status          ENUM('registered','cancelled','attended','no_show','late_cancel') NOT NULL DEFAULT 'registered',
    cancelled_at    DATETIME DEFAULT NULL,

    UNIQUE INDEX idx_user_event (user_id, em_event_id),
    INDEX idx_user_id (user_id),
    INDEX idx_em_event_id (em_event_id),
    INDEX idx_status (status),

    CONSTRAINT fk_bookings_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 7. visits — фактические посещения
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS visits (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id         INT UNSIGNED NOT NULL,
    em_event_id     BIGINT UNSIGNED NOT NULL,
    subscription_id INT UNSIGNED DEFAULT NULL COMMENT 'Списание с абонемента',
    marked_at       DATETIME NOT NULL,
    marked_by       INT UNSIGNED DEFAULT NULL COMMENT 'Кто отметил (users.id)',
    method          ENUM('qr','manual_teacher','manual_admin','self') NOT NULL DEFAULT 'qr',
    visit_type      ENUM('subscription','single','postpayment') NOT NULL DEFAULT 'subscription',
    status          ENUM('present','absent','cancelled') NOT NULL DEFAULT 'present',
    comment         TEXT DEFAULT NULL,

    INDEX idx_user_id (user_id),
    INDEX idx_em_event_id (em_event_id),
    INDEX idx_subscription_id (subscription_id),
    INDEX idx_marked_at (marked_at),

    CONSTRAINT fk_visits_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT fk_visits_subscription FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL,
    CONSTRAINT fk_visits_marked_by FOREIGN KEY (marked_by) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 8. payments — платежи
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id             INT UNSIGNED DEFAULT NULL,
    amount              DECIMAL(8,2) NOT NULL,
    currency            VARCHAR(3) NOT NULL DEFAULT 'BYN',
    method              ENUM('cash','erip','bepaid','webpay','other') NOT NULL DEFAULT 'cash',
    status              ENUM('pending','paid','failed','refunded') NOT NULL DEFAULT 'pending',
    subscription_type_id INT UNSIGNED DEFAULT NULL,
    subscription_id     INT UNSIGNED DEFAULT NULL,
    order_id            VARCHAR(100) DEFAULT NULL COMMENT 'ID заказа в платёжной системе',
    external_id         VARCHAR(100) DEFAULT NULL COMMENT 'ID транзакции',
    description         VARCHAR(255) NOT NULL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    paid_at             DATETIME DEFAULT NULL,

    INDEX idx_user_id (user_id),
    INDEX idx_status (status),
    INDEX idx_order_id (order_id),
    INDEX idx_created_at (created_at),

    CONSTRAINT fk_payments_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT fk_payments_type FOREIGN KEY (subscription_type_id) REFERENCES subscription_types(id) ON DELETE SET NULL,
    CONSTRAINT fk_payments_subscription FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 9. tasks — задачи для преподавателей (CRM)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    title           VARCHAR(255) NOT NULL,
    description     TEXT DEFAULT NULL,
    assigned_to     INT UNSIGNED NOT NULL,
    created_by      INT UNSIGNED NOT NULL,
    due_at          DATETIME DEFAULT NULL,
    status          ENUM('new','in_progress','done','cancelled') NOT NULL DEFAULT 'new',
    priority        ENUM('low','normal','high') NOT NULL DEFAULT 'normal',
    related_user_id INT UNSIGNED DEFAULT NULL COMMENT 'Если задача о конкретном ученике',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_assigned_to (assigned_to),
    INDEX idx_status (status),
    INDEX idx_due_at (due_at),

    CONSTRAINT fk_tasks_assigned FOREIGN KEY (assigned_to) REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT fk_tasks_created FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT fk_tasks_related_user FOREIGN KEY (related_user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 10. notifications — история уведомлений
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id     INT UNSIGNED NOT NULL,
    type        VARCHAR(100) NOT NULL COMMENT 'reminder, payment, freeze, birthday...',
    payload     JSON DEFAULT NULL,
    sent_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status      ENUM('sent','failed','read') NOT NULL DEFAULT 'sent',

    INDEX idx_user_id (user_id),
    INDEX idx_type (type),
    INDEX idx_sent_at (sent_at),

    CONSTRAINT fk_notifications_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 11. audit_log — лог действий
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id     INT UNSIGNED DEFAULT NULL COMMENT 'Кто сделал',
    action      VARCHAR(50) NOT NULL COMMENT 'create, update, delete',
    table_name  VARCHAR(100) NOT NULL,
    record_id   BIGINT UNSIGNED DEFAULT NULL,
    old_value   JSON DEFAULT NULL,
    new_value   JSON DEFAULT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_user_id (user_id),
    INDEX idx_table_name (table_name),
    INDEX idx_created_at (created_at),

    CONSTRAINT fk_audit_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
