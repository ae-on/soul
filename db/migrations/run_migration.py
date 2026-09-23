"""
Скрипт для применения миграций MySQL.

Запуск:
    python3 db/migrations/run_migration.py 001_add_user_roles.sql

Выполняет SQL из указанного файла и логирует результат.
"""

import logging
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from db.connection import get_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run_migration(filename: str):
    """Читает SQL-файл миграции и выполняет его."""
    migration_dir = Path(__file__).resolve().parent
    filepath = migration_dir / filename

    if not filepath.exists():
        logger.error("Файл миграции не найден: %s", filepath)
        sys.exit(1)

    sql = filepath.read_text(encoding="utf-8")
    if not sql.strip():
        logger.warning("Файл миграции пуст: %s", filename)
        return

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        logger.info("Миграция %s успешно применена", filename)
    except Exception as e:
        logger.error("Ошибка при применении миграции %s: %s", filename, e)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python3 db/migrations/run_migration.py <filename>")
        print("Пример: python3 db/migrations/run_migration.py 001_add_user_roles.sql")
        sys.exit(1)

    run_migration(sys.argv[1])
