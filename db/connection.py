"""
Подключение к MySQL (CRM soul_studio).

Использует PyMySQL для синхронных операций (init_db, миграции)
и aiomysql для асинхронных запросов из бота.
"""

import logging
import os

import pymysql
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Параметры подключения из .env
DB_CONFIG = {
    "host": os.getenv("STUDIO_DB_HOST", "localhost"),
    "port": int(os.getenv("STUDIO_DB_PORT", 3306)),
    "user": os.getenv("STUDIO_DB_USER", "soul_studio"),
    "password": os.getenv("STUDIO_DB_PASSWORD", ""),
    "database": os.getenv("STUDIO_DB_NAME", "soul_studio"),
    "charset": "utf8mb4",
}


def get_connection():
    """
    Синхронное подключение к MySQL.
    Используется в init_db.py и скриптах миграций.
    """
    try:
        conn = pymysql.connect(**DB_CONFIG)
        logger.info(
            "Подключение к MySQL: %s@%s:%d/%s",
            DB_CONFIG["user"],
            DB_CONFIG["host"],
            DB_CONFIG["port"],
            DB_CONFIG["database"],
        )
        return conn
    except pymysql.Error as e:
        logger.error("Ошибка подключения к MySQL: %s", e)
        raise


async def get_async_pool():
    """
    Асинхронный пул соединений aiomysql.
    Используется из бота для CRUD-операций.
    """
    try:
        import aiomysql

        pool = await aiomysql.create_pool(
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            db=DB_CONFIG["database"],
            charset="utf8mb4",
            autocommit=True,
        )
        logger.info("Асинхронный пул MySQL создан")
        return pool
    except ImportError:
        logger.error("aiomysql не установлен. Установите: pip install aiomysql")
        raise
    except Exception as e:
        logger.error("Ошибка создания пула MySQL: %s", e)
        raise
