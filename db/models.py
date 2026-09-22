"""
Модели для MySQL CRM soul_studio.

Базовый CRUD без ORM — только параметризованные SQL-запросы.
Каждый класс — обёртка над одной таблицей.
"""

import logging
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


class SubscriptionType(BaseModel):
    TABLE = "subscription_types"


class Subscription(BaseModel):
    TABLE = "subscriptions"


class Freeze(BaseModel):
    TABLE = "freezes"


class EventCache(BaseModel):
    TABLE = "events_cache"


class Booking(BaseModel):
    TABLE = "bookings"


class Visit(BaseModel):
    TABLE = "visits"


class Payment(BaseModel):
    TABLE = "payments"


class Task(BaseModel):
    TABLE = "tasks"


class Notification(BaseModel):
    TABLE = "notifications"


class AuditLog(BaseModel):
    TABLE = "audit_log"
