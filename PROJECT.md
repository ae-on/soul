# Проект: Telegram-бот для студии танцев и йоги

## Назначение
Бот для записи клиентов на занятия, управления абонементами и информирования.

## Технологии
- Python 3.11
- Библиотека: python-telegram-bot (20.x)
- База данных: SQLite (файл clients.db)
- Шифрование: cryptography (Fernet)
- Синхронизация: Git + GitHub

## Структура проекта
- `bot.py` — основной файл с обработчиками команд и меню
- `database.py` — работа с БД (создание, чтение, запись, шифрование)
- `requirements.txt` — зависимости
- `.env` — переменные окружения (токен бота, секретный ключ)
- `.gitignore` — исключаемые файлы

## Переменные окружения (.env)
- BOT_TOKEN — токен бота от @BotFather
- SECRET_KEY — ключ для шифрования Fernet (генерируется отдельно)

## Команды для запуска
- Установка зависимостей: `pip install -r requirements.txt`
- Запуск бота (polling): `python3 bot.py`

## Схема БД
Таблица `clients`:
- id — INTEGER PRIMARY KEY
- telegram_id — INTEGER UNIQUE NOT NULL
- full_name — TEXT (зашифровано)
- phone_number — TEXT (зашифровано)
- utm_source — TEXT (источник перехода)
- registration_date — DATETIME DEFAULT CURRENT_TIMESTAMP
- consent_given — BOOLEAN DEFAULT 0

## Как деплоить на VPS

1. **Обновить код:**
   ```bash
   git pull origin main
   ```

2. **Обновить зависимости:**
   ```bash
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Применить новые миграции БД:**
   ```bash
   python3 db/migrations/run_migration.py
   ```
   Скрипт сам найдёт все неприменённые файлы `.sql` в `db/migrations/` и выполнит их по порядку.
   Если нужно применить одну конкретную миграцию:
   ```bash
   python3 db/migrations/run_migration.py 009_add_teacher_id.sql
   ```

4. **Перезапустить бота:**
   ```bash
   sudo systemctl restart soul-bot
   ```

---

> **Примечание:** Если бот не запускается с ошибкой `RuntimeError: There is no current event loop` —
> убедитесь, что используется Python 3.12+ и обновлённый `bot.py` (с безопасной инициализацией цикла событий).
