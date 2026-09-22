"""
Инициализация MySQL-схемы CRM soul_studio.

Запуск: python3 db/init_db.py

Читает schema.sql, создаёт таблицы (IF NOT EXISTS),
заполняет справочник типов абонементов.
"""

import logging
from pathlib import Path

from db.connection import get_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Начальные данные для subscription_types
INITIAL_SUBSCRIPTION_TYPES = [
    (1, "Разовое", 1, 1, 25.00, 0, 0, 1, 1, 1),
    (2, "Разовое 1.5ч", 1, 1, 30.00, 0, 0, 1, 1, 2),
    (3, "4 занятия", 4, 30, 90.00, 1, 14, 1, 1, 3),
    (4, "4 занятия 1.5ч", 4, 30, 110.00, 1, 14, 1, 1, 4),
    (5, "8 занятий", 8, 30, 145.00, 1, 14, 1, 1, 5),
    (6, "8 занятий 1.5ч", 8, 30, 185.00, 1, 14, 1, 1, 6),
    (7, "12 занятий", 12, 30, 200.00, 1, 14, 1, 1, 7),
    (8, "Безлимит 30 дней", None, 30, 250.00, 1, 14, 1, 1, 8),
]


def init_database():
    """Создаёт таблицы и заполняет начальные данные."""
    # Читаем schema.sql
    if not SCHEMA_PATH.exists():
        logger.error("Файл schema.sql не найден: %s", SCHEMA_PATH)
        return

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    # Подключаемся
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Выполняем schema.sql (разделяем по комментариям CREATE TABLE)
            # Выполняем каждый CREATE TABLE отдельно, чтобы видеть ошибки
            statements = []
            current = []
            for line in schema_sql.split("\n"):
                if line.strip().upper().startswith("CREATE TABLE"):
                    if current:
                        statements.append("\n".join(current))
                    current = [line]
                else:
                    current.append(line)
            if current:
                statements.append("\n".join(current))

            created = 0
            already = 0
            for stmt in statements:
                if not stmt.strip():
                    continue
                try:
                    cur.execute(stmt)
                    # Пытаемся извлечь имя таблицы из сообщения
                    table_name = None
                    for line in stmt.split("\n"):
                        if "CREATE TABLE IF NOT EXISTS" in line.upper():
                            # users ( ...
                            parts = line.split("(")[0].split()
                            if parts:
                                table_name = parts[-1].strip()
                            break
                    if table_name:
                        logger.info("Таблица %s создана/проверена", table_name)
                    created += 1
                except Exception as e:
                    logger.warning("Ошибка при создании таблицы: %s", e)
                    already += 1

            logger.info(
                "Схема обработана: %d команд выполнено, %d пропущено", created, already
            )

            # Заполняем subscription_types
            for row in INITIAL_SUBSCRIPTION_TYPES:
                try:
                    cur.execute(
                        """
                        INSERT IGNORE INTO subscription_types
                            (id, name, visits_count, duration_days, price_byn,
                             freeze_count_max, freeze_days_max, cross_group,
                             active, sort_order)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        row,
                    )
                except Exception as e:
                    logger.warning(
                        "Ошибка при вставке типа абонемента %s: %s", row[1], e
                    )

            conn.commit()
            logger.info("Инициализация БД завершена")

    finally:
        conn.close()


if __name__ == "__main__":
    init_database()
