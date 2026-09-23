"""
Скрипт для применения миграций MySQL.

Поддерживает SQL-файлы с несколькими statement'ами, разделёнными `;`.
Комментарии `--` и `#` отбрасываются.

Запуск:
    python3 db/migrations/run_migration.py 001_add_user_roles.sql
"""

import logging
import re
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from db.connection import get_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def split_sql(sql_text: str) -> list[str]:
    """
    Разбивает SQL-текст на отдельные statement'ы по `;`,
    игнорируя однострочные комментарии (-- и #).
    """
    # Убираем однострочные комментарии
    sql_text = re.sub(r"--.*?$", "", sql_text, flags=re.MULTILINE)
    sql_text = re.sub(r"#.*?$", "", sql_text, flags=re.MULTILINE)
    # Разбиваем по точке с запятой
    parts = [s.strip() for s in sql_text.split(";")]
    # Убираем пустые части
    return [s for s in parts if s]


def run_migration(filename: str):
    """Читает SQL-файл миграции и выполняет каждый statement по отдельности."""
    migration_dir = Path(__file__).resolve().parent
    filepath = migration_dir / filename

    if not filepath.exists():
        logger.error("Файл миграции не найден: %s", filepath)
        sys.exit(1)

    sql_text = filepath.read_text(encoding="utf-8")
    if not sql_text.strip():
        logger.warning("Файл миграции пуст: %s", filename)
        return

    statements = split_sql(sql_text)
    if not statements:
        logger.warning("Нет SQL-запросов в файле: %s", filename)
        return

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for i, stmt in enumerate(statements, 1):
                cur.execute(stmt)
                logger.info("Statement %d/%d выполнен", i, len(statements))
        conn.commit()
        logger.info(
            "Миграция %s успешно применена (%d statement'ов)", filename, len(statements)
        )
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
