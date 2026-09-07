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

## Модуль регистрации
- Бот принимает `/start` с параметром utm_source.
- Сохраняет пользователя в таблицу clients (telegram_id, utm_source).
- Показывает главное меню.
- При нажатии «Записаться» запускается диалог (имя → телефон → сохранение в БД с шифрованием).
- Планируется: подтверждение записи, выбор даты/времени.
