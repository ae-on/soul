"""
Модели для MySQL CRM soul_studio.

Базовый CRUD без ORM — только параметризованные SQL-запросы.
Каждый класс — обёртка над одной таблицей.
"""

import logging
from datetime import datetime
from typing import Any, Optional

import pymysql
from pymysql.cursors import DictCursor

from db.connection import get_connection

logger = logging.getLogger(__name__)


class BaseModel:
    """Базовый класс с общими CRUD-методами."""

    TABLE = ""  # переопределяется в наследниках
    PK = "id"

    @classmethod
    def get_by_id(cls, record_id: int) -> Optional[dict]:
        """Найти запись по первичному ключу."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    f"SELECT * FROM {cls.TABLE} WHERE {cls.PK} = %s", (record_id,)
                )
                return cur.fetchone()

    @classmethod
    def get_by_field(cls, **kwargs) -> list[dict]:
        """
        Найти записи по произвольным полям.
        Пример: User.get_by_field(role='student', status='active')
        """
        if not kwargs:
            return []
        conditions = " AND ".join(f"{k} = %s" for k in kwargs)
        values = list(kwargs.values())
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(f"SELECT * FROM {cls.TABLE} WHERE {conditions}", values)
                return cur.fetchall()

    @classmethod
    def create(cls, **kwargs) -> int:
        """Создать запись, вернуть ID."""
        columns = ", ".join(kwargs.keys())
        placeholders = ", ".join("%s" for _ in kwargs)
        values = list(kwargs.values())
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {cls.TABLE} ({columns}) VALUES ({placeholders})",
                    values,
                )
                conn.commit()
                return cur.lastrowid

    @classmethod
    def update(cls, record_id: int, **kwargs) -> bool:
        """Обновить запись по PK. Вернуть True, если строка изменена."""
        if not kwargs:
            return False
        sets = ", ".join(f"{k} = %s" for k in kwargs)
        values = list(kwargs.values()) + [record_id]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {cls.TABLE} SET {sets} WHERE {cls.PK} = %s",
                    values,
                )
                conn.commit()
                return cur.rowcount > 0

    @classmethod
    def delete(cls, record_id: int) -> bool:
        """Удалить запись по PK."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {cls.TABLE} WHERE {cls.PK} = %s", (record_id,)
                )
                conn.commit()
                return cur.rowcount > 0


class User(BaseModel):
    TABLE = "users"

    @classmethod
    def get_roles(cls, user_id: int) -> list[str]:
        """
        Возвращает список активных ролей пользователя из таблицы user_roles.
        Пример: ['student', 'teacher']
        """
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT role FROM user_roles
                       WHERE user_id = %s AND active = 1""",
                    (user_id,),
                )
                return [row["role"] for row in cur.fetchall()]

    @classmethod
    def get_by_telegram_id(cls, telegram_id: int) -> Optional[dict]:
        """Возвращает пользователя по telegram_id или None."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    "SELECT * FROM users WHERE telegram_id = %s",
                    (telegram_id,),
                )
                return cur.fetchone()

    @classmethod
    def create_or_get(cls, telegram_id: int, username: str = None) -> dict:
        """
        Если пользователь есть — вернуть его.
        Если нет — создать с role='student', status='new'
        и сразу выдать роль student в user_roles.
        Возвращает dict пользователя.
        """
        existing = cls.get_by_telegram_id(telegram_id)
        if existing:
            return existing

        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """INSERT INTO users
                       (telegram_id, username, full_name, phone, role, status, registered_at)
                       VALUES (%s, %s, '', '', 'student', 'new', NOW())""",
                    (telegram_id, username),
                )
                user_id = cur.lastrowid
                conn.commit()

        # Выдаём роль student в user_roles
        UserRole.grant(user_id, "student")

        logger.info(
            "Создан новый пользователь: telegram_id=%s, id=%s", telegram_id, user_id
        )
        return cls.get_by_id(user_id)

    @classmethod
    def update_contacts(cls, user_id: int, full_name: str, phone: str) -> bool:
        """Обновить ФИО и телефон."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE users
                       SET full_name = %s, phone = %s
                       WHERE id = %s""",
                    (full_name, phone, user_id),
                )
                conn.commit()
                return cur.rowcount > 0

    @classmethod
    def set_consent(cls, user_id: int, version: str) -> bool:
        """
        Зафиксировать согласие на обработку ПДн.
        Устанавливает consent_given=1, consent_date=NOW(),
        consent_text_version=version.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE users
                       SET consent_given = 1,
                           consent_date = NOW(),
                           consent_text_version = %s
                       WHERE id = %s""",
                    (version, user_id),
                )
                conn.commit()
                return cur.rowcount > 0

    @classmethod
    def set_status(cls, user_id: int, status: str) -> bool:
        """Обновить статус (new, active, expired, archived, inactive)."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET status = %s WHERE id = %s",
                    (status, user_id),
                )
                conn.commit()
                return cur.rowcount > 0

    @classmethod
    def update_activity(cls, user_id: int) -> bool:
        """Обновить last_activity_at = NOW()."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET last_activity_at = NOW() WHERE id = %s",
                    (user_id,),
                )
                conn.commit()
                return cur.rowcount > 0

    @classmethod
    def list_by_status(
        cls, status_filter: str = None, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        """
        Список пользователей с фильтром по статусу,
        сортировка по registered_at DESC.
        """
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                if status_filter:
                    cur.execute(
                        """SELECT * FROM users
                           WHERE status = %s
                           ORDER BY registered_at DESC
                           LIMIT %s OFFSET %s""",
                        (status_filter, limit, offset),
                    )
                else:
                    cur.execute(
                        """SELECT * FROM users
                           WHERE status IN ('new', 'active', 'expired', 'archived')
                           ORDER BY registered_at DESC
                           LIMIT %s OFFSET %s""",
                        (limit, offset),
                    )
                return cur.fetchall()

    @classmethod
    def count_by_status(cls) -> dict:
        """
        Возвращает словарь с количеством пользователей по статусам.
        Ключи: new, active, expired, total.
        """
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT status, COUNT(*) as cnt FROM users
                       WHERE status IN ('new', 'active', 'expired', 'archived')
                       GROUP BY status"""
                )
                counts = {row["status"]: row["cnt"] for row in cur.fetchall()}
                cur.execute("SELECT COUNT(*) as total FROM users")
                counts["total"] = cur.fetchone()["total"]
                return counts

    @classmethod
    def get_by_role(
        cls,
        role: str,
        limit: int = 10,
        offset: int = 0,
        status_filter: str = "all",
    ) -> list[dict]:
        """
        Пользователи по роли (student/teacher/admin) из таблицы user_roles.
        status_filter: 'all', 'new', 'active'.
        Сортировка по full_name ASC.
        """
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                if status_filter == "all":
                    cur.execute(
                        """SELECT u.* FROM users u
                           JOIN user_roles ur ON ur.user_id = u.id
                           WHERE ur.role = %s AND ur.active = 1
                           ORDER BY u.full_name ASC
                           LIMIT %s OFFSET %s""",
                        (role, limit, offset),
                    )
                else:
                    cur.execute(
                        """SELECT u.* FROM users u
                           JOIN user_roles ur ON ur.user_id = u.id
                           WHERE ur.role = %s AND ur.active = 1
                             AND u.status = %s
                           ORDER BY u.full_name ASC
                           LIMIT %s OFFSET %s""",
                        (role, status_filter, limit, offset),
                    )
                return cur.fetchall()

    @classmethod
    def count_by_role(cls, role: str, status_filter: str = "all") -> int:
        """Количество пользователей с ролью (для пагинации)."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                if status_filter == "all":
                    cur.execute(
                        """SELECT COUNT(*) AS cnt FROM user_roles
                           WHERE role = %s AND active = 1""",
                        (role,),
                    )
                else:
                    cur.execute(
                        """SELECT COUNT(*) AS cnt
                           FROM user_roles ur
                           JOIN users u ON u.id = ur.user_id
                           WHERE ur.role = %s AND ur.active = 1
                             AND u.status = %s""",
                        (role, status_filter),
                    )
                row = cur.fetchone()
                return row["cnt"] if row else 0


class SubscriptionType(BaseModel):
    TABLE = "subscription_types"

    @classmethod
    def get_active(cls) -> list[dict]:
        """Все активные типы абонементов, по sort_order."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT * FROM subscription_types
                       WHERE active = 1
                       ORDER BY sort_order, id"""
                )
                return cur.fetchall()

    @classmethod
    def get_all(cls) -> list[dict]:
        """Все типы абонементов (включая неактивные), по sort_order."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    "SELECT * FROM subscription_types ORDER BY sort_order, id"
                )
                return cur.fetchall()


class Subscription(BaseModel):
    TABLE = "subscriptions"

    @classmethod
    def get_active_for_user(cls, user_id: int) -> Optional[dict]:
        """
        Активный абонемент пользователя:
        status='active', не истёк, есть часы (или безлимит).
        Возвращает dict или None.
        """
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT * FROM subscriptions
                       WHERE user_id = %s
                         AND status = 'active'
                         AND (expires_at IS NULL OR expires_at > NOW())
                         AND (hours_left IS NULL OR hours_left > 0)
                       ORDER BY created_at DESC
                       LIMIT 1""",
                    (user_id,),
                )
                return cur.fetchone()

    @classmethod
    def deduct_hours(cls, subscription_id: int, hours: float) -> bool:
        """
        Списать N часов с абонемента.
        Проверяет, что hours_left >= hours.
        Для безлимита (hours_left IS NULL) — всегда True.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Проверяем остаток
                cur.execute(
                    "SELECT hours_left FROM subscriptions WHERE id = %s",
                    (subscription_id,),
                )
                row = cur.fetchone()
                if not row:
                    return False
                if row["hours_left"] is None:
                    # Безлимит — списываем без проверки
                    return True
                # DECIMAL из MySQL приходит как Decimal — приводим к float
                if float(row["hours_left"]) < hours:
                    return False
                cur.execute(
                    "UPDATE subscriptions SET hours_left = hours_left - %s WHERE id = %s",
                    (hours, subscription_id),
                )
                conn.commit()
                return cur.rowcount > 0

    @classmethod
    def refund_hours(cls, subscription_id: int, hours: float) -> bool:
        """
        Вернуть N часов на абонемент (при отмене занятия).
        Для безлимита (hours_total IS NULL) — ничего не делаем.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT hours_total FROM subscriptions WHERE id = %s",
                    (subscription_id,),
                )
                row = cur.fetchone()
                if not row:
                    return False
                if row["hours_total"] is None:
                    # Безлимит — возвращать нечего
                    return True
                cur.execute(
                    "UPDATE subscriptions SET hours_left = hours_left + %s WHERE id = %s",
                    (hours, subscription_id),
                )
                conn.commit()
                return cur.rowcount > 0

    @classmethod
    def extend_expires(cls, subscription_id: int, days: int) -> bool:
        """
        Сдвинуть expires_at на N дней вперёд (при заморозке).
        Для абонементов без срока (expires_at IS NULL) —
        срок считается от текущего момента.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE subscriptions
                       SET expires_at = DATE_ADD(
                           COALESCE(expires_at, NOW()), INTERVAL %s DAY
                       )
                       WHERE id = %s""",
                    (days, subscription_id),
                )
                conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    logger.info(
                        "Абонемент %s продлён на %s дней", subscription_id, days
                    )
                return ok

    @classmethod
    def get_for_user(cls, user_id: int) -> list[dict]:
        """Все абонементы пользователя, новые сверху (для админки)."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT * FROM subscriptions
                       WHERE user_id = %s
                       ORDER BY created_at DESC""",
                    (user_id,),
                )
                return cur.fetchall()

    @classmethod
    def update_hours(cls, sub_id: int, delta: float) -> bool:
        """
        Изменить hours_left на delta (не ниже 0).
        Для безлимита (hours_left IS NULL) — ничего не меняем, True.
        """
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    "SELECT hours_left FROM subscriptions WHERE id = %s",
                    (sub_id,),
                )
                row = cur.fetchone()
                if not row:
                    return False
                if row["hours_left"] is None:
                    # Безлимит — часы не списываются и не добавляются
                    return True
                # DECIMAL из MySQL приходит как Decimal — приводим к float
                current_hours = float(row["hours_left"])
                new_value = max(0.0, current_hours + delta)
                cur.execute(
                    "UPDATE subscriptions SET hours_left = %s WHERE id = %s",
                    (new_value, sub_id),
                )
                conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    logger.info(
                        "Абонемент %s: hours_left %s -> %s",
                        sub_id,
                        row["hours_left"],
                        new_value,
                    )
                return ok

    @classmethod
    def update_expires(cls, sub_id: int, new_expires: datetime) -> bool:
        """Обновить expires_at (new_expires — datetime)."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET expires_at = %s WHERE id = %s",
                    (new_expires, sub_id),
                )
                conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    logger.info(
                        "Абонемент %s: expires_at -> %s", sub_id, new_expires
                    )
                return ok

    @classmethod
    def set_status(cls, sub_id: int, status: str) -> bool:
        """Обновить статус абонемента (active/frozen/expired/depleted/cancelled)."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET status = %s WHERE id = %s",
                    (status, sub_id),
                )
                conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    logger.info("Абонемент %s: статус -> %s", sub_id, status)
                return ok

    @classmethod
    def update_freeze_used(cls, sub_id: int, delta: int = 1) -> bool:
        """Изменить счётчик использованных заморозок на delta (обычно +1)."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET freeze_used = GREATEST(0, freeze_used + %s) WHERE id = %s",
                    (delta, sub_id),
                )
                conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    logger.info("Абонемент %s: freeze_used %+d", sub_id, delta)
                return ok


class Freeze(BaseModel):
    TABLE = "freezes"

    @classmethod
    def get_active_for_subscription(cls, subscription_id: int) -> dict | None:
        """
        Активная заморозка (status='approved' и end_date >= CURDATE()).
        Возвращает dict или None.
        """
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT * FROM freezes
                       WHERE subscription_id = %s
                         AND status = 'approved'
                         AND end_date >= CURDATE()
                       ORDER BY end_date DESC
                       LIMIT 1""",
                    (subscription_id,),
                )
                return cur.fetchone()

    @classmethod
    def get_open_for_subscription(cls, subscription_id: int) -> dict | None:
        """
        Незавершённая заморозка абонемента: pending (заявка) или approved (активная).
        Возвращает dict или None.
        """
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT * FROM freezes
                       WHERE subscription_id = %s
                         AND status IN ('pending', 'approved')
                       ORDER BY created_at DESC
                       LIMIT 1""",
                    (subscription_id,),
                )
                return cur.fetchone()

    @classmethod
    def get_approved_started(cls, subscription_id: int) -> dict | None:
        """
        Одобренная заморозка, которая уже началась (start_date <= CURDATE()).
        Используется для автозавершения при возврате клиента.
        """
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT * FROM freezes
                       WHERE subscription_id = %s
                         AND status = 'approved'
                         AND start_date <= CURDATE()
                       ORDER BY end_date DESC
                       LIMIT 1""",
                    (subscription_id,),
                )
                return cur.fetchone()

    @classmethod
    def get_pending(cls) -> list[dict]:
        """Все заявки со status='pending'."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT * FROM freezes
                       WHERE status = 'pending'
                       ORDER BY created_at"""
                )
                return cur.fetchall()

    @classmethod
    def approve(cls, freeze_id: int, admin_id: int) -> bool:
        """status='approved', reviewed_by=admin_id, reviewed_at=NOW()."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE freezes
                       SET status = 'approved',
                           reviewed_by = %s,
                           reviewed_at = NOW()
                       WHERE id = %s""",
                    (admin_id, freeze_id),
                )
                conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    logger.info(
                        "Заморозка %s одобрена админом %s", freeze_id, admin_id
                    )
                return ok

    @classmethod
    def reject(cls, freeze_id: int, admin_id: int) -> bool:
        """status='rejected', reviewed_by=admin_id, reviewed_at=NOW()."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE freezes
                       SET status = 'rejected',
                           reviewed_by = %s,
                           reviewed_at = NOW()
                       WHERE id = %s""",
                    (admin_id, freeze_id),
                )
                conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    logger.info(
                        "Заморозка %s отклонена админом %s", freeze_id, admin_id
                    )
                return ok

    @classmethod
    def cancel(cls, freeze_id: int) -> bool:
        """status='cancelled'. Для отмены клиентом."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE freezes
                       SET status = 'cancelled'
                       WHERE id = %s""",
                    (freeze_id,),
                )
                conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    logger.info("Заморозка %s отменена", freeze_id)
                return ok

    @classmethod
    def complete(cls, freeze_id: int, actual_end_date, days_actual: int) -> bool:
        """status='completed', actual_end_date, days_actual."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE freezes
                       SET status = 'completed',
                           actual_end_date = %s,
                           days_actual = %s
                       WHERE id = %s""",
                    (actual_end_date, days_actual, freeze_id),
                )
                conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    logger.info(
                        "Заморозка %s завершена: %s дней", freeze_id, days_actual
                    )
                return ok


class EventCache(BaseModel):
    TABLE = "events_cache"


class Booking(BaseModel):
    TABLE = "bookings"

    @classmethod
    def get_active_for_user(cls, user_id: int) -> list[dict]:
        """Все активные записи пользователя (не cancelled/late_cancel/no_show)."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT * FROM bookings
                       WHERE user_id = %s
                         AND status IN ('registered', 'pending', 'confirmed')
                       ORDER BY event_date, event_time""",
                    (user_id,),
                )
                return cur.fetchall()

    @classmethod
    def get_by_user_and_event(cls, user_id: int, em_event_id: int) -> Optional[dict]:
        """Найти запись конкретного пользователя на событие."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    "SELECT * FROM bookings WHERE user_id = %s AND em_event_id = %s",
                    (user_id, em_event_id),
                )
                return cur.fetchone()

    @classmethod
    def get_by_event(cls, em_event_id: int) -> list[dict]:
        """Все записи на конкретное событие (для преподавателя)."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT b.*, u.full_name, u.phone, u.username
                       FROM bookings b
                       JOIN users u ON u.id = b.user_id
                       WHERE b.em_event_id = %s
                       ORDER BY b.created_at""",
                    (em_event_id,),
                )
                return cur.fetchall()

    @classmethod
    def count_confirmed_for_event(cls, em_event_id: int) -> int:
        """Количество записанных (для проверки лимита)."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT COUNT(*) as cnt FROM bookings
                       WHERE em_event_id = %s
                         AND status IN ('registered', 'pending', 'confirmed')""",
                    (em_event_id,),
                )
                row = cur.fetchone()
                return row["cnt"] if row else 0


class Visit(BaseModel):
    TABLE = "visits"

    @classmethod
    def get_by_user(cls, user_id: int, limit: int = 20) -> list[dict]:
        """
        Последние посещения пользователя, DESC по marked_at.
        JOIN с events_cache по em_event_id для получения названия.
        """
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT v.*, e.name AS event_name,
                              e.start_date AS event_date,
                              e.start_time AS event_time
                       FROM visits v
                       LEFT JOIN events_cache e ON e.em_event_id = v.em_event_id
                       WHERE v.user_id = %s
                       ORDER BY v.marked_at DESC
                       LIMIT %s""",
                    (user_id, limit),
                )
                return cur.fetchall()


class Payment(BaseModel):
    TABLE = "payments"


class Task(BaseModel):
    TABLE = "tasks"


class Notification(BaseModel):
    TABLE = "notifications"


class AuditLog(BaseModel):
    TABLE = "audit_log"


class UserRole(BaseModel):
    """Мультироли пользователей."""

    TABLE = "user_roles"

    @classmethod
    def get_by_user_id(cls, user_id: int) -> list[dict]:
        """Все роли пользователя (активные и неактивные)."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    "SELECT * FROM user_roles WHERE user_id = %s ORDER BY role",
                    (user_id,),
                )
                return cur.fetchall()

    @classmethod
    def get_active_roles(cls, user_id: int) -> list[str]:
        """Список только активных ролей."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT role FROM user_roles
                       WHERE user_id = %s AND active = 1""",
                    (user_id,),
                )
                return [row["role"] for row in cur.fetchall()]

    @classmethod
    def grant(cls, user_id: int, role: str, granted_by: int = None) -> int:
        """
        Выдать роль пользователю.
        Если запись уже существует — обновить active=1 и granted_by.
        Возвращает ID записи.
        """
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                # Проверяем, есть ли уже такая запись
                cur.execute(
                    "SELECT id FROM user_roles WHERE user_id = %s AND role = %s",
                    (user_id, role),
                )
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        """UPDATE user_roles
                           SET active = 1, granted_by = %s
                           WHERE id = %s""",
                        (granted_by, existing["id"]),
                    )
                    conn.commit()
                    return existing["id"]
                else:
                    cur.execute(
                        """INSERT INTO user_roles (user_id, role, granted_by)
                           VALUES (%s, %s, %s)""",
                        (user_id, role, granted_by),
                    )
                    conn.commit()
                    return cur.lastrowid

    @classmethod
    def revoke(cls, user_id: int, role: str) -> bool:
        """Отозвать роль (установить active=0). Вернуть True, если строка изменена."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE user_roles SET active = 0
                       WHERE user_id = %s AND role = %s AND active = 1""",
                    (user_id, role),
                )
                conn.commit()
                return cur.rowcount > 0


class Group(BaseModel):
    """Слоты (регулярные классы)."""

    TABLE = "slots"

    @classmethod
    def get_active(cls) -> list[dict]:
        """Все активные слоты."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    f"SELECT * FROM {cls.TABLE} WHERE active = 1 ORDER BY category_slug, time_start"
                )
                return cur.fetchall()

    @classmethod
    def get_by_teacher(cls, teacher_id: int) -> list[dict]:
        """Слоты конкретного преподавателя."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    f"SELECT * FROM {cls.TABLE} WHERE teacher_id = %s AND active = 1 ORDER BY time_start",
                    (teacher_id,),
                )
                return cur.fetchall()

    @classmethod
    def get_by_category(cls, category_slug: str) -> list[dict]:
        """Слоты по дисциплине."""
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    f"SELECT * FROM {cls.TABLE} WHERE category_slug = %s AND active = 1 ORDER BY time_start",
                    (category_slug,),
                )
                return cur.fetchall()

    @classmethod
    def get_by_weekday(cls, weekday: int) -> list[dict]:
        """
        Слоты, которые идут в указанный день недели.
        weekday: 1=Пн, 2=Вт, ..., 7=Вс.
        Ищем вхождение числа в строку weekdays (например, "1,3").
        """
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    f"""SELECT * FROM {cls.TABLE}
                       WHERE active = 1
                         AND FIND_IN_SET(%s, weekdays) > 0
                       ORDER BY time_start""",
                    (str(weekday),),
                )
                return cur.fetchall()

    @staticmethod
    def calculate_duration(time_start, time_end) -> float:
        """Вычисляет длительность в часах (1.0 или 1.5)."""
        from datetime import datetime, timedelta

        # time_start/time_end могут быть timedelta или строкой
        if isinstance(time_start, str):
            ref = datetime.strptime("00:00:00", "%H:%M:%S")
            start = datetime.strptime(str(time_start), "%H:%M:%S")
            end = datetime.strptime(str(time_end), "%H:%M:%S")
        else:
            # timedelta
            ref = datetime.min
            start = ref + time_start
            end = ref + time_end

        diff = (end - start).total_seconds() / 3600
        return round(diff, 1)

    @classmethod
    def create_with_duration(cls, **kwargs) -> int:
        """
        Создаёт слот, автоматически вычисляя duration_hours
        из time_start и time_end.
        """
        time_start = kwargs.pop("time_start")
        time_end = kwargs.pop("time_end")
        kwargs["duration_hours"] = cls.calculate_duration(time_start, time_end)
        return cls.create(**kwargs)
