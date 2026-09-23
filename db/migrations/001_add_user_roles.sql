-- =============================================================================
-- Миграция 001: таблица user_roles (мультироли)
-- =============================================================================

CREATE TABLE IF NOT EXISTS user_roles (
    id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id     INT UNSIGNED NOT NULL,
    role        ENUM('student','teacher','admin','super_admin') NOT NULL,
    granted_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    granted_by  INT UNSIGNED DEFAULT NULL,
    active      TINYINT(1) NOT NULL DEFAULT 1,

    PRIMARY KEY (id),
    UNIQUE KEY uq_user_role (user_id, role),
    KEY idx_role_active (role, active),
    KEY idx_user (user_id),

    CONSTRAINT fk_user_roles_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_user_roles_granted_by
        FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
