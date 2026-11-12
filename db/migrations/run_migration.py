"""
Скрипт для применения миграций MySQL с учётом уже выполненных.

Режимы запуска:
    python3 db/migrations/run_migration.py              — применить все неприменённые миграции
    python3 db/migrations/run_migration.py <file.sql>   — применить одну конкретную миграцию

Скрипт идемпотентен: повторный запуск не наносит вреда.
"""
import glob
import logging
import os
import re
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from db.connection import get_connection

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent


def split_sql(sql_text: str) -> list[str]:
    """
    Разбивает SQL-текст на отдельные statement'ы по `;`,
    игнорируя однострочные комментарии (-- и #).
    """
    sql_text = re.sub(r"--.*?$", "", sql_text, flags=re.MULTILINE)
    sql_text = re.sub(r"#.*?$", "", sql_text, flags=re.MULTILINE)
    parts = [s.strip() for s in sql_text.split(";")]
    return [s for s in parts if s]


def ensure_migrations_table():
    """Создаёт schema_migrations, если её нет."""
    sql = (MIGRATIONS_DIR / "000_init_migrations_table.sql").read_text(encoding="utf-8")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for stmt in split_sql(sql):
                if stmt:
                    cur.execute(stmt)
        conn.commit()
    except Exception as e:
        logger.error("Не удалось создать таблицу schema_migrations: %s", e)
        sys.exit(1)
    finally:
        conn.close()


def get_applied() -> set[str]:
    """Возвращает множество имён уже применённых миграций."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations")
            return {row[0] for row in cur.fetchall()}
    except Exception:
        return set()
    finally:
        conn.close()


def apply_migration(filename: str, sql_text: str) -> bool:
    """
    Выполняет SQL из файла. В случае ошибки откатывает и НЕ записывает
    миграцию в schema_migrations. Возвращает True при успехе.
    """
    statements = split_sql(sql_text)
    if not statements:
        logger.warning("Нет SQL-запросов в файле: %s", filename)
        return False

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for i, stmt in enumerate(statements, 1):
                cur.execute(stmt)
        conn.commit()
        logger.info("Миграция %s применена (%d statement'ов)", filename, len(statements))
        return True
    except Exception as e:
        conn.rollback()
        logger.error("Ошибка при выполнении миграции %s: %s", filename, e)
        return False
    finally:
        conn.close()


def record_migration(filename: str):
    """Записывает имя файла в schema_migrations."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO schema_migrations (version) VALUES (%s)",
                (filename,),
            )
        conn.commit()
    except Exception as e:
        logger.error("Не удалось записать миграцию %s: %s", filename, e)
    finally:
        conn.close()


def run_single(filename: str) -> bool:
    """Применить одну миграцию по имени файла."""
    filepath = MIGRATIONS_DIR / filename
    if not filepath.exists():
        logger.error("Файл не найден: %s", filepath)
        return False

    applied = get_applied()
    if filename in applied:
        logger.info("Миграция %s уже применена. Пропуск.", filename)
        return True

    sql_text = filepath.read_text(encoding="utf-8")
    if not sql_text.strip():
        logger.warning("Файл миграции пуст: %s", filename)
        return True

    if apply_migration(filename, sql_text):
        record_migration(filename)
        return True

    logger.error("Миграция %s НЕ записана — исправьте ошибку и запустите снова.", filename)
    return False


def run_all():
    """Применить все неприменённые миграции (кроме 000_*)."""
    ensure_migrations_table()
    applied = get_applied()

    # Собираем все *.sql, исключаем 000_*
    pattern = os.path.join(MIGRATIONS_DIR, "[0-9]*.sql")
    files = sorted(
        f for f in glob.glob(pattern)
        if not os.path.basename(f).startswith("000_")
    )

    if not files:
        logger.info("Нет файлов миграций для применения.")
        return

    new_count = 0
    for filepath in files:
        filename = os.path.basename(filepath)
        if filename in applied:
            logger.debug("Миграция %s уже применена, пропускаем", filename)
            continue

        sql_text = Path(filepath).read_text(encoding="utf-8")
        if not sql_text.strip():
            logger.warning("Файл миграции пуст: %s", filename)
            continue

        if apply_migration(filename, sql_text):
            record_migration(filename)
            new_count += 1

    total = len(applied) + new_count
    logger.info(
        "Применено новых миграций: %s. Всего в базе: %s.", new_count, total
    )


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        # Режим одной миграции: предварительно создаём таблицу учёта
        ensure_migrations_table()
        ok = run_single(sys.argv[1])
        sys.exit(0 if ok else 1)
    else:
        # Пакетный режим: применить всё
        run_all()