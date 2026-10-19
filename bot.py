import asyncio
import logging
import math
import os
import re
import warnings
from datetime import date, datetime, timedelta
from typing import Optional


import aiohttp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Подавляем предупреждение PTBUserWarning от ConversationHandler
# (per_message=False по умолчанию, но для нашего диалога это неважно)
warnings.filterwarnings(
    "ignore",
    message=".*If 'per_message=False'.*",
    category=UserWarning,
)

from db.connection import get_connection
from db.constants import CONSENT_TEXT, CONSENT_TEXT_VERSION
from db.models import (
    Booking,
    Freeze,
    Group,
    Subscription,
    SubscriptionType,
    User,
    UserRole,
    Visit,
)
from rag_agent import ask, sync_knowledge

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# WordPress API
WP_URL = os.getenv("WP_URL", "https://soul.by")
WP_USER = os.getenv("WP_USER", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def safe_answer(query, text=None, show_alert=False):
    """Безопасно отвечает на callback, игнорируя временные сетевые ошибки."""
    try:
        await query.answer(text=text, show_alert=show_alert)
    except Exception as e:
        logger.warning("Не удалось ответить на callback (%s): %s", type(e).__name__, e)


def _format_date(value) -> str:
    """Форматирует дату как ДД.ММ.ГГГГ; None превращает в «—»."""
    if value is None:
        return "—"
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y")
    return str(value)[:10]


def _format_time(value) -> str:
    """Форматирует время как ЧЧ:ММ (из timedelta/time/строки); None — «—»."""
    if value is None:
        return "—"
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    # PyMySQL возвращает TIME как timedelta: str() может быть "9:30:00"
    parts = str(value).split(":")
    if len(parts) >= 2:
        return f"{int(parts[0]):02d}:{parts[1]}"
    return str(value)


def _extract_int(data: str, prefix: str) -> Optional[int]:
    """Достаёт число из суффикса callback-строки; None при неудаче."""
    try:
        return int(data[len(prefix):])
    except (ValueError, IndexError):
        return None


def _extract_two_ints(data: str, prefix: str) -> Optional[tuple[int, int]]:
    """Достаёт два числа, разделённых '_'; None при неудаче."""
    parts = data[len(prefix):].split("_", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


async def notify_admin(
    context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None
) -> bool:
    """Отправляет уведомление администратору (ADMIN_CHAT_ID из .env)."""
    if not ADMIN_CHAT_ID:
        logger.info("ADMIN_CHAT_ID не задан — уведомление не отправлено")
        return False
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID, text=text, reply_markup=reply_markup
        )
        return True
    except Exception as e:
        logger.error("Не удалось отправить уведомление админу: %s", e)
        return False


# Состояния для диалога регистрации
CONSENT, FULL_NAME, PHONE = range(3)

# Состояния для диалога редактирования профиля
PROFILE_FULL_NAME, PROFILE_PHONE = range(2)

# Состояние диалога смены даты окончания абонемента (админка)
ADMIN_SUB_EXPIRES_DATE = 10

# Инициализируем FTS5 (база знаний) при старте
sync_knowledge()


# ---------------------------------------------------------------------------
# WordPress API: получение и форматирование расписания
# ---------------------------------------------------------------------------


async def get_upcoming_events() -> list[dict]:
    """
    GET /wp-json/events-manager/v1/events
    Возвращает список событий или [] при ошибке.
    """
    url = f"{WP_URL}/wp-json/events-manager/v1/events"
    auth = (
        aiohttp.BasicAuth(WP_USER, WP_APP_PASSWORD)
        if WP_USER and WP_APP_PASSWORD
        else None
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                auth=auth,
                timeout=10,
                params={
                    "scope": "future",
                    "limit": 100,
                    "orderby": "event_start_date",
                    "order": "ASC",
                },
            ) as resp:
                data = await resp.json()
                items = data.get("items", [])
                logger.info("WP Events: получено %d событий", len(items))
                return items
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error("Ошибка при получении расписания из WP: %s", e)
        return []


def filter_events_by_period(events: list[dict], period: str) -> list[dict]:
    """
    Фильтрует события по when.start_date.
    period: 'today' | 'tomorrow' | 'week' | 'next7'
    Возвращает отсортированный по (дата, время) список.
    """
    today = date.today()

    if period == "today":
        cutoff_start = today
        cutoff_end = today + timedelta(days=1)
    elif period == "tomorrow":
        cutoff_start = today + timedelta(days=1)
        cutoff_end = today + timedelta(days=2)
    elif period == "week":
        # от сегодня до воскресенья включительно
        days_until_sun = (6 - today.weekday()) % 7
        cutoff_start = today
        cutoff_end = today + timedelta(days=days_until_sun + 1)
    elif period == "next7":
        cutoff_start = today
        cutoff_end = today + timedelta(days=7)
    else:
        return []

    filtered = []
    for ev in events:
        start_date_str = ev.get("when", {}).get("start_date", "")
        if not start_date_str:
            continue
        try:
            ev_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if cutoff_start <= ev_date < cutoff_end:
            filtered.append(ev)

    # Сортировка: сначала по дате, потом по времени
    def sort_key(ev):
        when = ev.get("when", {})
        d = when.get("start_date", "")
        t = when.get("start_time", "00:00:00")
        return (d, t)

    filtered.sort(key=sort_key)
    return filtered


# Маппинг slug -> эмодзи
CATEGORY_EMOJI = {
    "bachata": "\U0001f483",
    "yoga": "\U0001f9d8",
    "tribal": "\U0001f525",
    "stretching": "\U0001f938",
}

CATEGORY_NAMES = {
    "bachata": "Бачата",
    "tribal": "Трайбл",
    "yoga": "Йога",
    "stretching": "Стретчинг",
    "qigong": "Цигун",
    "oriental": "Восточный танец",
    "indian": "Индийский танец",
    "historical": "Исторический танец",
    "pilates": "Пилатес",
}

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Маппинг: slug направления → slug тега в Events Manager
# Если тег ещё не создан — ставим None
DIRECTION_TO_TAG = {
    "bachata": "bachata",
    "tribal": "tribal",
    "yoga": "yoga",
    "pilates": "pilates",
    "stretching": None,
    "qigong": "qigong",
    "oriental": "vostok",
    "indian": "indian",
    "historical": "historical",
}

# Обратный маппинг: tag_slug → название направления
TAG_TO_NAME = {
    "bachata": "Бачата",
    "tribal": "Трайбл",
    "yoga": "Йога",
    "pilates": "Пилатес",
    "stretching": "Стретчинг",
    "qigong": "Цигун",
    "vostok": "Восточный танец",
    "indian": "Индийский танец",
    "historical": "Исторический танец",
}

# Обратный маппинг: tag_slug → direction_slug (для кнопки "К направлению")
TAG_TO_DIRECTION = {v: k for k, v in DIRECTION_TO_TAG.items() if v}


def filter_events_by_category(events: list[dict], slug: str) -> list[dict]:
    """
    Фильтрует события по slug категории (регистронезависимо).
    """
    slug_lower = slug.lower()
    result = []
    for ev in events:
        for cat in ev.get("categories", []):
            if cat.get("slug", "").lower() == slug_lower:
                result.append(ev)
                break
    return result


def format_event(ev: dict) -> str:
    """Форматирует одно событие в строку для Telegram."""
    when = ev.get("when", {})
    start_date_str = when.get("start_date", "")
    start_time_str = when.get("start_time", "")

    # Дата: только день недели "Пн"
    try:
        dt = datetime.strptime(start_date_str, "%Y-%m-%d")
        day_name = WEEKDAYS_RU[dt.weekday()]
        date_line = day_name
    except ValueError:
        date_line = start_date_str

    # Время: "16:30"
    time_line = start_time_str[:5] if start_time_str else ""

    # Эмодзи по первой категории
    categories = ev.get("categories", [])
    slug = categories[0].get("slug", "") if categories else ""
    emoji = CATEGORY_EMOJI.get(slug, "\U0001f3ab")

    # Название
    name = ev.get("name", "Без названия")

    # Места
    bookings = ev.get("bookings", {})
    if bookings.get("enabled"):
        available = bookings.get("available_spaces", 0)
        if available > 0:
            spaces_str = f"· {available} мест"
        else:
            spaces_str = "· ❌ мест нет"
    else:
        spaces_str = ""

    # Локация
    location_str = ""
    location = ev.get("location")
    if location and isinstance(location, dict) and location.get("name"):
        location_str = f"· \U0001f4cd {location['name']}"

    parts = [
        f"{date_line} \u00b7 {time_line}" if time_line else date_line,
        f"{emoji} {name}",
    ]
    if location_str:
        parts.append(location_str)
    if spaces_str:
        parts.append(spaces_str)

    return " \u00b7 ".join(parts)


# ---------------------------------------------------------------------------
# Названия месяцев в родительном падеже
# ---------------------------------------------------------------------------
MONTHS_GENITIVE = [
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]


# ---------------------------------------------------------------------------
# Построение ряда кнопок дней (скользящее окно 7 дней)
# ---------------------------------------------------------------------------
def build_days_keyboard(
    selected_date: date = None, tag_slug: str = None
) -> list[list[InlineKeyboardButton]]:
    """
    Строит ряд из 7 inline-кнопок: сегодня + 6 следующих дней.
    Если tag_slug указан — добавляет '#slug' к callback_data.
    """
    today = date.today()
    if selected_date is None:
        selected_date = today

    suffix = f"#{tag_slug}" if tag_slug else ""

    row = []
    for i in range(7):
        day = today + timedelta(days=i)
        day_name = WEEKDAYS_RU[day.weekday()]
        day_num = day.day

        if day == selected_date:
            if day == today:
                label = f"[\u00b7{day_name} {day_num}\u00b7]"
            else:
                label = f"[{day_name} {day_num}]"
        elif day == today:
            label = f"\u00b7{day_name}\u00b7"
        else:
            label = day_name

        row.append(
            InlineKeyboardButton(
                label,
                callback_data=f"schedule_day_{day.isoformat()}{suffix}",
            )
        )

    return [row]


# ---------------------------------------------------------------------------
# Показ расписания на конкретный день (плоский экран, глубина 1)
# ---------------------------------------------------------------------------
async def show_schedule_day(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    date_str: str = None,
    tag_slug: str = None,
):
    """
    Показывает расписание на один день.
    Если tag_slug задан — показывает только события с этим тегом.
    """
    query = update.callback_query
    await safe_answer(query)

    # Определяем выбранную дату
    if date_str:
        try:
            selected_date = date.fromisoformat(date_str)
        except ValueError:
            selected_date = date.today()
    else:
        selected_date = date.today()

    today = date.today()

    # Получаем события
    events = await get_upcoming_events()

    # Фильтр по тегу (если задан)
    if tag_slug:
        tag_slug_lower = tag_slug.lower()
        before = len(events)
        events = [
            ev
            for ev in events
            if any(
                t.get("slug", "").lower() == tag_slug_lower for t in ev.get("tags", [])
            )
        ]
        logger.info(
            "Фильтр по тегу '%s': до=%d, после=%d",
            tag_slug,
            before,
            len(events),
        )

    day_events = []
    target_str = selected_date.isoformat()
    for ev in events:
        w = ev.get("when", {})
        if w.get("start_date") == target_str:
            day_events.append(ev)

    # Сортировка по времени
    day_events.sort(key=lambda ev: ev.get("when", {}).get("start_time", ""))

    # Заголовок верхний (с тегом, если есть)
    if tag_slug:
        tag_name = TAG_TO_NAME.get(tag_slug, tag_slug.capitalize())
        top_title = f"\U0001f4c5 Расписание \u00b7 {tag_name}"
    else:
        top_title = "\U0001f4c5 Расписание"

    lines = [top_title, ""]

    if not day_events:
        if tag_slug:
            tag_name = TAG_TO_NAME.get(tag_slug, tag_slug.capitalize())
            lines.append(f"Пока нет занятий по направлению {tag_name} в этот день.")
        else:
            lines.append("Пока нет занятий в этот день.")
    else:
        for ev in day_events:
            when = ev.get("when", {})
            time_str = when.get("start_time", "")[:5]

            categories = ev.get("categories", [])
            slug = categories[0].get("slug", "") if categories else ""
            emoji = CATEGORY_EMOJI.get(slug, "\U0001f3ab")

            name = ev.get("name", "Без названия")

            location_str = ""
            location = ev.get("location")
            if location and isinstance(location, dict) and location.get("name"):
                location_str = f" \u00b7 \U0001f4cd {location['name']}"

            line = f"\U0001f555 {time_str} \u00b7 {emoji} {name}{location_str}"
            lines.append(line)

    # Заголовок дня — ПОСЛЕ списка событий
    weekday_name = WEEKDAYS_RU[selected_date.weekday()]
    month_name = MONTHS_GENITIVE[selected_date.month]
    header = f"\U0001f5d3 {weekday_name}, {selected_date.day} {month_name}"
    if selected_date == today:
        header += " \u00b7 \U0001f5d3 Сегодня"
    lines.append("")
    lines.append(header)

    text = "\n".join(lines)

    # Клавиатура
    keyboard = build_days_keyboard(selected_date, tag_slug)

    # Кнопки записи: 2 столбца (Зал 1 слева, Зал 2 справа)
    left_events = []
    right_events = []
    for ev in day_events:
        loc = ev.get("location")
        loc_name = ""
        if loc and isinstance(loc, dict) and loc.get("name"):
            loc_name = loc["name"]
        if "Зал 2" in loc_name:
            right_events.append(ev)
        else:
            left_events.append(ev)

    max_len = max(len(left_events), len(right_events))
    for i in range(max_len):
        left_btn = None
        right_btn = None

        if i < len(left_events):
            ev = left_events[i]
            time_str = ev.get("when", {}).get("start_time", "")[:5]
            name = ev.get("name", "")
            left_btn = InlineKeyboardButton(
                f"\u270d\ufe0f {time_str} {name}",
                callback_data=f"book_event_{ev['id']}",
            )
        if i < len(right_events):
            ev = right_events[i]
            time_str = ev.get("when", {}).get("start_time", "")[:5]
            name = ev.get("name", "")
            right_btn = InlineKeyboardButton(
                f"\u270d\ufe0f {time_str} {name}",
                callback_data=f"book_event_{ev['id']}",
            )

        row = []
        if left_btn:
            row.append(left_btn)
        else:
            row.append(InlineKeyboardButton(" ", callback_data="noop"))
        if right_btn:
            row.append(right_btn)
        else:
            row.append(InlineKeyboardButton(" ", callback_data="noop"))
        keyboard.append(row)

    # Навигация
    if tag_slug:
        # direction_slug для кнопки возврата
        direction_slug = TAG_TO_DIRECTION.get(tag_slug)
        if direction_slug:
            # Ищем callback_key в DIRECTION_META
            back_to_direction = None
            for c_key, (c_slug, c_emoji, c_name) in DIRECTION_META.items():
                if c_slug == direction_slug:
                    back_to_direction = c_key
                    break
            if back_to_direction is None:
                back_to_direction = f"direction_{direction_slug}"

            keyboard.append(
                [
                    InlineKeyboardButton(
                        "\U0001f519 К направлению", callback_data=back_to_direction
                    ),
                    InlineKeyboardButton(
                        "\U0001f3e0 Главное меню", callback_data="back_to_main"
                    ),
                ]
            )
        else:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "\U0001f3e0 Главное меню", callback_data="back_to_main"
                    )
                ]
            )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f3e0 Главное меню", callback_data="back_to_main"
                )
            ]
        )

    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


# ---------------------------------------------------------------------------
# Безопасное редактирование сообщения (игнорирует BadRequest "not modified")
# ---------------------------------------------------------------------------
async def _safe_edit(query, text: str, reply_markup=None):
    """Вызывает edit_message_text, игнорируя ошибку 'Message is not modified'."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise


async def book_event_stub(
    update: Update, context: ContextTypes.DEFAULT_TYPE, event_id: int
):
    """Обработка нажатия «Записаться» на занятие — логика по статусу."""
    query = update.callback_query
    await safe_answer(query)

    telegram_id = update.effective_user.id
    user = User.get_by_telegram_id(telegram_id)
    status = user["status"] if user else "new"
    email = None

    if status in ("new", "deleted_data"):
        # Гость — предлагаем регистрацию
        context.user_data["pending_event_id"] = event_id
        text = (
            "\u26a0\ufe0f Чтобы записаться на занятие, нужно пройти регистрацию.\n\n"
            "После регистрации вы сразу сможете записаться на это занятие.\n"
            "Продолжить?"
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "\u270d\ufe0f Зарегистрироваться", callback_data="book"
                )
            ],
            [InlineKeyboardButton("\U0001f519 К расписанию", callback_data="schedule")],
        ]
    elif status in ("expired", "archived", "inactive"):
        text = "\u26a0\ufe0f Ваш абонемент закончился. Купите новый, чтобы записаться."
        keyboard = [
            [
                InlineKeyboardButton(
                    "\U0001f6cd\ufe0f Купить абонемент", callback_data="prices"
                )
            ],
        ]
    else:
        # Активный ученик — заглушка записи
        text = (
            f"\u270d\ufe0f Запись на событие #{event_id}\n\n"
            "Запись откроется в ближайшее время."
        )
        keyboard = [
            [InlineKeyboardButton("\U0001f519 К расписанию", callback_data="schedule")],
        ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# Маппинг: callback_data_info -> (slug, эмодзи, короткое имя)
# Используется для построения кнопок расписания в описании направления
DIRECTION_META = {
    "tribal_info": ("tribal", "\U0001f525", "Трайбл фьюжн"),
    "oriental_info": ("oriental", "\U0001fa70", "Восточный танец"),
    "yoga_info": ("yoga", "\U0001f9d8", "Йога"),
    "bachata_info": ("bachata", "\U0001f483", "Бачата"),
    "qigong_info": ("qigong", "\U0001f33f", "Цигун"),
    "indian_info": ("indian", "\U0001f1ee\U0001f1f3", "Индийский танец"),
    "historical_info": ("historical", "\U0001f3db\ufe0f", "Исторический танец"),
    "pilates_info": ("pilates", "\U0001f9d8", "Пилатес"),
}


async def show_direction_schedule(
    update: Update, context: ContextTypes.DEFAULT_TYPE, slug: str
):
    """
    Показывает расписание по направлению (slug) на ближайшие 7 дней.
    """
    query = update.callback_query
    await safe_answer(query)

    # Определяем meta по slug (обратный поиск)
    callback_key = None
    name = slug.capitalize()
    emoji = "\U0001f3ab"
    for c_key, (c_slug, c_emoji, c_name) in DIRECTION_META.items():
        if c_slug == slug:
            callback_key = c_key
            name = c_name
            emoji = c_emoji
            break

    # Получаем и фильтруем события
    events = await get_upcoming_events()
    cat_events = filter_events_by_category(events, slug)
    filtered = filter_events_by_period(cat_events, "next7")

    title = f"{emoji} {name} — ближайшие 7 дней"

    if not filtered:
        text = f"{title}\n\nПока нет занятий по направлению {name} в ближайшие 7 дней."
        keyboard = [
            [
                InlineKeyboardButton(
                    "\U0001f519 Назад",
                    callback_data=callback_key or f"direction_{slug}",
                )
            ],
            [
                InlineKeyboardButton(
                    "\U0001f3e0 Главное меню", callback_data="back_to_main"
                )
            ],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Сортируем (filter_events_by_period уже сортирует, но подстрахуемся)
    def sort_key(ev):
        w = ev.get("when", {})
        return (w.get("start_date", ""), w.get("start_time", ""))

    filtered.sort(key=sort_key)

    lines = [f"**{title}**\n"]
    for ev in filtered:
        lines.append(format_event(ev))

    text = "\n\n".join(lines)

    keyboard = []
    for ev in filtered:
        ev_id = ev.get("id", 0)
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"\u2705 Записаться — {ev.get('name', '')}",
                    callback_data=f"book_event_{ev_id}",
                )
            ]
        )

    back_btn = callback_key if callback_key else f"direction_{slug}"
    keyboard.append(
        [
            InlineKeyboardButton("\U0001f519 Назад", callback_data=back_btn),
            InlineKeyboardButton(
                "\U0001f3e0 Главное меню", callback_data="back_to_main"
            ),
        ]
    )

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def show_main_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None
):
    """Отправляет (или редактирует) главное меню."""
    keyboard = [
        [InlineKeyboardButton("📅 Расписание и запись", callback_data="schedule")],
        [InlineKeyboardButton("💰 Абонементы и цены", callback_data="prices")],
        [InlineKeyboardButton("📍 О студии", callback_data="info")],
        [InlineKeyboardButton("🧭 Направления", callback_data="directions")],
        [InlineKeyboardButton("💬 Задать вопрос", callback_data="support")],
        [InlineKeyboardButton("✍️ Записаться на занятие", callback_data="book")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if text is None:
        text = "Выберите действие в меню ниже:"

    # Если это callback — редактируем сообщение, иначе — отправляем новое
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


# ----------------------------------------------------------------------
# Инфо-меню для гостя (status='new')
# ----------------------------------------------------------------------
async def show_info_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None
):
    """Меню для гостя (status='new'/'deleted_data')."""
    keyboard = [
        [InlineKeyboardButton("\U0001f4c5 Расписание", callback_data="schedule")],
        [InlineKeyboardButton("\U0001f9ed Направления", callback_data="directions")],
        [InlineKeyboardButton("\U0001f4b0 Абонементы и цены", callback_data="prices")],
        [InlineKeyboardButton("\u2139\ufe0f О студии", callback_data="info")],
        [InlineKeyboardButton("\U0001f4ac Задать вопрос", callback_data="support")],
        [InlineKeyboardButton("\u270d\ufe0f Регистрация", callback_data="book")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if text is None:
        text = "\U0001f3e0 Меню"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


# ----------------------------------------------------------------------
# Меню по статусу пользователя
# ----------------------------------------------------------------------
async def show_menu_by_role(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict, text: str = None
):
    """
    Отображает меню в зависимости от статуса пользователя и его ролей.
    Для new/deleted_data — инфо-меню, для остальных — меню ученика.
    """
    # Если статус new или deleted_data — инфо-меню
    if user["status"] in ("new", "deleted_data"):
        await show_info_menu(update, context, text=text or "\U0001f3e0 Меню")
        return

    user_id = user["id"]
    roles = User.get_roles(user_id)

    keyboard = []

    # Роли (админ/преподаватель) добавляются сверху, если есть
    if "super_admin" in roles or "admin" in roles:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f527 Панель админа", callback_data="admin_panel"
                )
            ]
        )
    if "teacher" in roles:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f9d1\u200d\U0001f3eb Панель преподавателя",
                    callback_data="teacher_panel",
                )
            ]
        )

    # Ученик (active / expired / archived / inactive)
    keyboard.append(
        [
            InlineKeyboardButton(
                "\U0001f4c5 Расписание и запись", callback_data="schedule"
            )
        ]
    )
    keyboard.append(
        [InlineKeyboardButton("\U0001f9ed Направления", callback_data="directions")]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                "\U0001f4b0 Мой абонемент", callback_data="my_subscription"
            )
        ]
    )
    keyboard.append(
        [InlineKeyboardButton("\U0001f464 Профиль", callback_data="profile")]
    )
    keyboard.append(
        [InlineKeyboardButton("\u2139\ufe0f О студии", callback_data="info")]
    )
    keyboard.append(
        [InlineKeyboardButton("\U0001f4ac Поддержка", callback_data="support")]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)

    if text is None:
        text = "\U0001f3e0 Меню"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


# ----------------------------------------------------------------------
# Профиль пользователя
# ----------------------------------------------------------------------
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает профиль пользователя (работает и от кнопки, и из диалога)."""
    query = update.callback_query
    if query:
        await safe_answer(query)

    telegram_id = update.effective_user.id
    user = User.get_by_telegram_id(telegram_id)
    if not user:
        if query:
            await query.edit_message_text("Ошибка загрузки профиля. Нажмите /start.")
        else:
            await update.message.reply_text("Ошибка загрузки профиля. Нажмите /start.")
        return

    # Поля могут быть пустыми строками '' — трактуем как «не указано»
    full_name = user.get("full_name") or ""
    phone = user.get("phone") or ""

    roles = UserRole.get_active_roles(user["id"])
    roles_str = ", ".join(roles) if roles else "—"

    text = (
        "\U0001f464 Профиль\n\n"
        f"ФИО: {full_name or 'не указано'}\n"
        f"Телефон: {phone or 'не указан'}\n"
        f"Telegram: @{user.get('username') or user.get('telegram_id')}\n"
        f"Статус: {user.get('status')}\n"
        f"Роли: {roles_str}\n"
        f"Зарегистрирован: {_format_date(user.get('registered_at'))}"
    )
    keyboard = [
        [InlineKeyboardButton("\u270f\ufe0f Изменить данные", callback_data="profile_edit")],
        [
            InlineKeyboardButton(
                "\U0001f5d1\ufe0f Удалить данные", callback_data="confirm_delete"
            )
        ],
        [InlineKeyboardButton("\U0001f519 В меню", callback_data="back_to_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if query:
        await query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def confirm_delete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрос подтверждения удаления данных."""
    query = update.callback_query
    await safe_answer(query)

    text = (
        "\u26a0\ufe0f Удалить ФИО и телефон?\n\n"
        "Обезличенная запись (telegram_id, username) останется для истории.\n"
        "Вы сможете зарегистрироваться снова в любой момент."
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "\u2705 Да, удалить", callback_data="profile_delete_yes"
            )
        ],
        [InlineKeyboardButton("\u274c Отмена", callback_data="profile")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def profile_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Окончательное удаление данных пользователя."""
    query = update.callback_query
    await safe_answer(query)

    telegram_id = update.effective_user.id
    user = User.get_by_telegram_id(telegram_id)
    if user:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE users SET
                        full_name = '',
                        phone = '',
                        consent_given = 0,
                        consent_date = NULL,
                        consent_text_version = NULL,
                        status = 'deleted_data'
                    WHERE id = %s""",
                    (user["id"],),
                )
                conn.commit()
        logger.info("Пользователь %s удалил свои данные", user["id"])

    await query.edit_message_text(
        "\U0001f5d1\ufe0f Ваши данные удалены.\n\n"
        "Вы можете зарегистрироваться снова через меню."
    )
    await show_info_menu(update, context)


RULES_URL = "https://soul.by/ceni-na-tanci-minsk/"


# ----------------------------------------------------------------------
# Абонемент пользователя
# ----------------------------------------------------------------------
async def show_my_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает информацию об абонементе пользователя."""
    query = update.callback_query
    await safe_answer(query)

    telegram_id = update.effective_user.id
    user = User.get_by_telegram_id(telegram_id)
    if not user:
        await query.edit_message_text("Ошибка загрузки. Нажмите /start.")
        return

    sub = Subscription.get_active_for_user(user["id"])

    # --- Нет активного абонемента ---
    if not sub:
        text = "\U0001f4b0 Абонемент\n\nУ вас нет активного абонемента."
        keyboard = [
            [
                InlineKeyboardButton(
                    "\U0001f3e2 Купить в студии", callback_data="sub_buy_offline"
                )
            ],
            [
                InlineKeyboardButton(
                    "\U0001f6d2 Купить онлайн (ЕРИП)", callback_data="sub_buy_online"
                )
            ],
            [InlineKeyboardButton("\U0001f4d6 Правила пользования", url=RULES_URL)],
            [InlineKeyboardButton("\U0001f519 В меню", callback_data="back_to_main")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- Есть активный абонемент ---
    sub_type = SubscriptionType.get_by_id(sub["type_id"]) if sub.get("type_id") else None
    type_name = sub_type["name"] if sub_type else "Абонемент"

    hours_left = sub.get("hours_left")
    lines = [
        "\U0001f4b0 Мой абонемент",
        "",
        f"\U0001f4ca {type_name}",
    ]
    if hours_left is None:
        # NULL допустим только у безлимитного тарифа; иначе — ошибка данных
        if sub_type and "безлимит" in (sub_type.get("name") or "").lower():
            lines.append("\u23f3 Безлимит")
        else:
            lines.append("\u23f3 Осталось: 0ч (ошибка данных)")
    else:
        # DECIMAL из MySQL приходит как Decimal — перед математикой приводим к float
        hours_left_f = float(hours_left)
        n_one = math.floor(hours_left_f / 1.0)
        n_half = math.floor(hours_left_f / 1.5)
        lines.append(f"Осталось: {hours_left_f} часов")
        lines.append(f"≈ {n_one} занятий по 1ч или {n_half} по 1.5ч")

    expires_at = sub.get("expires_at")
    if expires_at:
        days_left = (expires_at.date() - date.today()).days
        lines.append(
            f"Действует до: {_format_date(expires_at)} ({days_left} дней)"
        )
    else:
        lines.append("Действует до: без ограничений")

    freeze_used = sub.get("freeze_used") or 0
    freeze_count_max = sub_type["freeze_count_max"] if sub_type else 0
    lines.append(
        f"Использовано заморозок: {freeze_used} из {freeze_count_max}"
    )
    text = "\n".join(lines)

    keyboard = [
        [InlineKeyboardButton("\U0001f4c5 История посещений", callback_data="sub_history")],
        [InlineKeyboardButton("\U0001f9ca Заморозить", callback_data="sub_freeze")],
        [
            InlineKeyboardButton(
                "\U0001f3e2 Продлить в студии", callback_data="sub_buy_offline"
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f6d2 Продлить онлайн (ЕРИП)", callback_data="sub_buy_online"
            )
        ],
        [InlineKeyboardButton("\U0001f4d6 Правила пользования", url=RULES_URL)],
    ]

    # Активная заморозка — дополнительный блок и кнопка отмены
    freeze = Freeze.get_active_for_subscription(sub["id"])
    if freeze:
        freeze_days_left = (freeze["end_date"] - date.today()).days
        text += (
            "\n\n\U0001f9ca Активная заморозка: "
            f"с {_format_date(freeze['start_date'])} "
            f"по {_format_date(freeze['end_date'])}\n"
            f"(осталось {freeze_days_left} дней)"
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\u274c Отменить заморозку", callback_data="sub_freeze_cancel"
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("\U0001f519 В меню", callback_data="back_to_main")]
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def show_subscription_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """История посещений пользователя (последние 20)."""
    query = update.callback_query
    await safe_answer(query)

    telegram_id = update.effective_user.id
    user = User.get_by_telegram_id(telegram_id)
    if not user:
        await query.edit_message_text("Ошибка загрузки. Нажмите /start.")
        return

    visits = Visit.get_by_user(user["id"], limit=20)

    if not visits:
        text = "\U0001f4c5 История посещений\n\nПока нет посещений."
    else:
        lines = ["\U0001f4c5 История посещений (последние 20)", ""]
        for v in visits:
            visit_status = (v.get("status") or "").lower()
            event_name = v.get("event_name") or f"Событие #{v.get('em_event_id')}"
            event_date = _format_date(v.get("event_date") or v.get("marked_at"))
            hours = v.get("hours_charged")
            hours_str = f"{hours}ч" if hours is not None else "—"

            if visit_status in ("present", "attended", "registered"):
                icon = "\u2705"
                line = f"{event_date} · {event_name} · {hours_str}"
            elif visit_status == "cancelled":
                icon = "\u274c"
                line = f"{event_date} · {event_name} · отменено"
            else:
                # absent / no_show / late_cancel / skipped
                icon = "\u26a0\ufe0f"
                line = f"{event_date} · {event_name} · пропуск"
            lines.append(f"{icon} {line}")
        text = "\n".join(lines)

    keyboard = [
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="my_subscription")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ----------------------------------------------------------------------
# Заглушки: заморозка, продление, покупка (полная логика — позже)
# ----------------------------------------------------------------------
FREEZE_REASONS = {
    "vacation": "\U0001f3d6\ufe0f Отпуск",
    "business": "\U0001f4bc Командировка",
    "sick": "\U0001f3e5 Болезнь",
    "other": "\u270f\ufe0f Другое",
}

FREEZE_MAX_DAYS = 14


def _get_user_and_active_sub(update: Update) -> tuple[Optional[dict], Optional[dict]]:
    """Возвращает (user, активный абонемент) или (None, None)."""
    user = User.get_by_telegram_id(update.effective_user.id)
    if not user:
        return None, None
    return user, Subscription.get_active_for_user(user["id"])


def _calc_freeze_period(expires_at) -> tuple[date, date, int]:
    """
    Рассчитывает период заморозки: start=сегодня, end=min(сегодня+14, срок), days.
    Возвращает (start_date, end_date, days) с инклюзивным подсчётом дней.
    """
    start = date.today()
    end = start + timedelta(days=FREEZE_MAX_DAYS)
    if expires_at is not None:
        exp_date = expires_at.date() if isinstance(expires_at, datetime) else expires_at
        end = min(end, exp_date)
    if end < start:
        end = start
    days = (end - start).days + 1
    return start, end, days


async def show_sub_freeze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Клиентский сценарий заморозки — проверки и выбор причины."""
    query = update.callback_query
    await safe_answer(query)

    user, sub = _get_user_and_active_sub(update)
    if not user or not sub:
        text = "\u26a0\ufe0f У вас нет активного абонемента."
        keyboard = [
            [InlineKeyboardButton("\U0001f519 В меню", callback_data="back_to_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    sub_type = (
        SubscriptionType.get_by_id(sub["type_id"]) if sub.get("type_id") else None
    )
    max_freezes = sub_type["freeze_count_max"] if sub_type else 0
    if (sub.get("freeze_used") or 0) >= max_freezes:
        text = "Вы уже использовали заморозку в этом абонементе."
        keyboard = [
            [InlineKeyboardButton("\U0001f519 Назад", callback_data="my_subscription")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if Freeze.get_open_for_subscription(sub["id"]):
        text = "\u26a0\ufe0f У вас уже есть активная заморозка или заявка на рассмотрении."
        keyboard = [
            [InlineKeyboardButton("\U0001f519 Назад", callback_data="my_subscription")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    type_name = sub_type["name"] if sub_type else "Абонемент"
    expires_at = sub.get("expires_at")
    days_left = (expires_at.date() - date.today()).days if expires_at else None
    days_left_str = f"{days_left} дней" if days_left is not None else "без лимита"

    text = (
        "\U0001f9ca Заморозка абонемента\n\n"
        f"Абонемент: {type_name}\n"
        f"Действует до: {_format_date(expires_at)} ({days_left_str})\n"
        f"Доступно: до {FREEZE_MAX_DAYS} дней (или меньше, "
        "если абонемент заканчивается раньше).\n\n"
        "Выберите причину:"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                FREEZE_REASONS["vacation"], callback_data="freeze_reason_vacation"
            ),
            InlineKeyboardButton(
                FREEZE_REASONS["business"], callback_data="freeze_reason_business"
            ),
        ],
        [
            InlineKeyboardButton(
                FREEZE_REASONS["sick"], callback_data="freeze_reason_sick"
            ),
            InlineKeyboardButton(
                FREEZE_REASONS["other"], callback_data="freeze_reason_other"
            ),
        ],
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="my_subscription")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def show_sub_freeze_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE, reason_key: str
):
    """Расчёт периода заморозки и экран подтверждения."""
    query = update.callback_query
    await safe_answer(query)

    user, sub = _get_user_and_active_sub(update)
    if not user or not sub:
        await query.edit_message_text("\u26a0\ufe0f Активный абонемент не найден.")
        return

    start, end, days = _calc_freeze_period(sub.get("expires_at"))
    reason_label = FREEZE_REASONS.get(reason_key, reason_key)

    # Запоминаем причину — на экране подтверждения кнопка freeze_confirm без данных
    context.user_data["pending_freeze_reason"] = reason_key

    text = (
        "\U0001f9ca Заморозка абонемента\n\n"
        f"\U0001f4c5 Заморозка: с {_format_date(start)} "
        f"по {_format_date(end)} ({days} дней)\n"
        f"Причина: {reason_label}\n\n"
        "После подтверждения админом абонемент будет продлён "
        f"на {days} дней."
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "\u2705 Подтвердить", callback_data="freeze_confirm"
            )
        ],
        [InlineKeyboardButton("\U0001f519 Отмена", callback_data="sub_freeze")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def submit_freeze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание заявки на заморозку и уведомление админу."""
    query = update.callback_query
    await safe_answer(query)

    # Причина была сохранена на предыдущем шаге (экран подтверждения)
    reason_key = context.user_data.pop("pending_freeze_reason", None)
    if not reason_key or reason_key not in FREEZE_REASONS:
        await query.edit_message_text(
            "\u26a0\ufe0f Действие устарело. Нажмите «\U0001f9ca Заморозить» заново."
        )
        return

    user, sub = _get_user_and_active_sub(update)
    if not user or not sub:
        await query.edit_message_text("\u26a0\ufe0f Активный абонемент не найден.")
        return
    if Freeze.get_open_for_subscription(sub["id"]):
        await query.edit_message_text(
            "\u26a0\ufe0f У вас уже есть активная заморозка или заявка на рассмотрении."
        )
        return

    start, end, days = _calc_freeze_period(sub.get("expires_at"))
    reason_label = FREEZE_REASONS.get(reason_key, reason_key)

    # days — generated column в MySQL, её НЕ передаём
    freeze_id = Freeze.create(
        subscription_id=sub["id"],
        start_date=start,
        end_date=end,
        reason=reason_label,
        created_by=user["id"],
        status="pending",
    )
    logger.info(
        "Заявка на заморозку %s: пользователь %s, период %s — %s (%s дней)",
        freeze_id,
        user["id"],
        start,
        end,
        days,
    )

    # Уведомление админу с кнопками подтверждения/отклонения.
    # ФИО и telegram_id берём из БД (поле full_name таблицы users)
    full_name = user.get("full_name") or ""
    admin_msg = (
        "\U0001f9ca Заявка на заморозку\n\n"
        f"Клиент: {full_name or '—'} (ID {user.get('telegram_id')})\n"
        f"Абонемент: #{sub['id']}\n"
        f"Период: {_format_date(start)} — {_format_date(end)} ({days} дней)\n"
        f"Причина: {reason_label}"
    )
    admin_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\u2705 Подтвердить", callback_data=f"freeze_approve_{freeze_id}"
                ),
                InlineKeyboardButton(
                    "\u274c Отклонить", callback_data=f"freeze_reject_{freeze_id}"
                ),
            ]
        ]
    )
    await notify_admin(context, admin_msg, reply_markup=admin_keyboard)

    text = "\u2705 Заявка на заморозку отправлена. Ждите подтверждения администратора."
    keyboard = [
        [InlineKeyboardButton("\U0001f519 В меню", callback_data="my_subscription")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_freeze_approve(
    update: Update, context: ContextTypes.DEFAULT_TYPE, freeze_id: int
):
    """Админ одобряет заявку на заморозку."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    freeze = Freeze.get_by_id(freeze_id)
    if not freeze:
        await safe_answer(query, "Заявка не найдена", show_alert=True)
        return
    if freeze["status"] != "pending":
        await query.edit_message_text("\u26a0\ufe0f Заявка уже обработана.")
        return

    Freeze.approve(freeze_id, admin["id"])
    Subscription.update_freeze_used(freeze["subscription_id"], 1)
    logger.info("Админ %s одобрил заморозку %s", admin["id"], freeze_id)

    # Уведомляем клиента
    sub = Subscription.get_by_id(freeze["subscription_id"])
    client = User.get_by_id(sub["user_id"]) if sub else None
    if client and client.get("telegram_id"):
        try:
            await context.bot.send_message(
                chat_id=client["telegram_id"],
                text=(
                    "\u2705 Ваша заморозка подтверждена "
                    f"с {_format_date(freeze['start_date'])} "
                    f"по {_format_date(freeze['end_date'])}."
                ),
            )
        except Exception as e:
            logger.error(
                "Не удалось уведомить клиента %s: %s", client["telegram_id"], e
            )

    # Обновляем сообщение в чате админа
    await query.edit_message_text(
        "\u2705 Заморозка одобрена\n\n"
        f"\U0001f9ca {_format_date(freeze['start_date'])} — "
        f"{_format_date(freeze['end_date'])}"
    )


async def handle_freeze_reject(
    update: Update, context: ContextTypes.DEFAULT_TYPE, freeze_id: int
):
    """Админ отклоняет заявку на заморозку."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    freeze = Freeze.get_by_id(freeze_id)
    if not freeze:
        await safe_answer(query, "Заявка не найдена", show_alert=True)
        return
    if freeze["status"] != "pending":
        await query.edit_message_text("\u26a0\ufe0f Заявка уже обработана.")
        return

    Freeze.reject(freeze_id, admin["id"])
    logger.info("Админ %s отклонил заморозку %s", admin["id"], freeze_id)

    # Уведомляем клиента
    sub = Subscription.get_by_id(freeze["subscription_id"])
    client = User.get_by_id(sub["user_id"]) if sub else None
    if client and client.get("telegram_id"):
        try:
            await context.bot.send_message(
                chat_id=client["telegram_id"],
                text="\u274c Администратор отклонил заморозку.",
            )
        except Exception as e:
            logger.error(
                "Не удалось уведомить клиента %s: %s", client["telegram_id"], e
            )

    await query.edit_message_text(
        "\u274c Заморозка отклонена\n\n"
        f"\U0001f9ca {_format_date(freeze['start_date'])} — "
        f"{_format_date(freeze['end_date'])}"
    )


async def show_sub_freeze_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена активной заморозки клиентом (срок абонемента не меняется)."""
    query = update.callback_query
    await safe_answer(query)

    user, sub = _get_user_and_active_sub(update)
    if not user or not sub:
        await query.edit_message_text("\u26a0\ufe0f Активный абонемент не найден.")
        return

    freeze = Freeze.get_active_for_subscription(sub["id"])
    if not freeze:
        text = "У вас нет активной заморозки."
    else:
        Freeze.cancel(freeze["id"])
        logger.info(
            "Пользователь %s отменил заморозку %s", user["id"], freeze["id"]
        )
        text = "\u274c Заморозка отменена."

    keyboard = [
        [InlineKeyboardButton("\U0001f519 В меню", callback_data="my_subscription")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


def check_and_complete_freeze(user_id: int) -> bool:
    """
    Автозавершение активной заморозки при возврате клиента.
    Фиксирует фактическую дату окончания и продлевает expires_at.
    Вызывается на /start и при отметке посещения.
    Возвращает True, если заморозка завершена.
    """
    sub = Subscription.get_active_for_user(user_id)
    if not sub:
        return False

    freeze = Freeze.get_approved_started(sub["id"])
    if not freeze:
        return False

    today = date.today()
    start = freeze["start_date"]
    if isinstance(start, datetime):
        start = start.date()
    end = freeze["end_date"]
    if isinstance(end, datetime):
        end = end.date()

    # Фактическое окончание — сегодня, но не позже запланированного
    actual_end = min(today, end)
    days_actual = max(1, (actual_end - start).days + 1)

    Freeze.complete(freeze["id"], actual_end, days_actual)
    Subscription.extend_expires(sub["id"], days_actual)
    logger.info(
        "Заморозка завершена досрочно, абонемент %s продлен на %s дней",
        sub["id"],
        days_actual,
    )
    return True


async def show_sub_buy_offline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Заявка на покупку (или продление) абонемента в студии."""
    query = update.callback_query
    await safe_answer(query)

    telegram_id = update.effective_user.id
    user = User.get_by_telegram_id(telegram_id)

    # Определяем: продление текущего абонемента или покупка нового
    purchase_text = "Новый абонемент"
    if user:
        sub = Subscription.get_active_for_user(user["id"])
        if sub:
            sub_type = (
                SubscriptionType.get_by_id(sub["type_id"])
                if sub.get("type_id")
                else None
            )
            purchase_text = (
                f"Продление: {sub_type['name'] if sub_type else 'текущий абонемент'}"
            )

    # Уведомление администратору
    if user:
        admin_msg = (
            "\U0001f3e2 Заявка на покупку абонемента\n\n"
            f"Клиент: {user.get('full_name') or '—'} (ID {telegram_id})\n"
            f"Телефон: {user.get('phone') or '—'}\n"
            f"Абонемент: {purchase_text}\n\n"
            "Свяжитесь для оформления (нал/безнал)."
        )
        await notify_admin(context, admin_msg)
        logger.info("Заявка на покупку абонемента от пользователя %s", user["id"])

    text = (
        "\u2705 Заявка отправлена.\n\n"
        "Администратор свяжется с вами для оформления "
        "и уточнения оплаты."
    )
    keyboard = [[InlineKeyboardButton("\U0001f519 В меню", callback_data="back_to_main")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def show_sub_buy_online(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Онлайн-оплата через ЕРИП — заглушка (интеграция позже)."""
    query = update.callback_query
    await safe_answer(query)

    text = (
        "\U0001f6d2 Онлайн-оплата временно недоступна.\n\n"
        "Скоро добавим оплату через ЕРИП. А пока — "
        "оформите покупку в студии."
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f3e2 Купить в студии", callback_data="sub_buy_offline"
            )
        ],
        [InlineKeyboardButton("\U0001f519 В меню", callback_data="back_to_main")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ----------------------------------------------------------------------
# Редактирование профиля (ConversationHandler)
# ----------------------------------------------------------------------
async def profile_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1 — показываем текущие данные, просим новое ФИО."""
    query = update.callback_query
    await safe_answer(query)

    telegram_id = update.effective_user.id
    user = User.get_by_telegram_id(telegram_id)
    if not user:
        await query.edit_message_text("Ошибка. Нажмите /start.")
        return ConversationHandler.END

    context.user_data["profile_edit_user_id"] = user["id"]
    context.user_data["profile_edit_old"] = {
        "full_name": user.get("full_name") or "",
        "phone": user.get("phone") or "",
    }

    text = (
        "\u270f\ufe0f Изменение данных\n\n"
        f"Текущее ФИО: {user.get('full_name') or 'не указано'}\n"
        f"Текущий телефон: {user.get('phone') or 'не указан'}\n\n"
        "Введите новое ФИО (или «-» чтобы оставить):"
    )
    keyboard = [[InlineKeyboardButton("\U0001f519 Отмена", callback_data="profile_cancel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return PROFILE_FULL_NAME


async def profile_edit_full_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2 — сохраняем ФИО, запрашиваем телефон."""
    new_name = update.message.text.strip()

    if new_name != "-":
        if len(new_name) < 3:
            await update.message.reply_text(
                "\u26a0\ufe0f ФИО должно содержать минимум 3 символа. "
                "Попробуйте ещё раз (или «-» чтобы оставить текущее):"
            )
            return PROFILE_FULL_NAME
        context.user_data["profile_edit_new_name"] = new_name
    else:
        context.user_data["profile_edit_new_name"] = None

    await update.message.reply_text(
        "Введите новый телефон (или «-» чтобы оставить без изменений):"
    )
    return PROFILE_PHONE


async def profile_edit_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3 — валидация телефона, сохранение, уведомление админу."""
    new_phone = update.message.text.strip()

    if new_phone != "-":
        digits = re.sub(r"\D", "", new_phone)
        if len(digits) < 7:
            await update.message.reply_text(
                "\u26a0\ufe0f В телефоне должно быть минимум 7 цифр "
                "(можно с + и пробелами). Попробуйте ещё раз "
                "(или «-» чтобы оставить текущий):"
            )
            return PROFILE_PHONE
        context.user_data["profile_edit_new_phone"] = new_phone
    else:
        context.user_data["profile_edit_new_phone"] = None

    user_id = context.user_data.get("profile_edit_user_id")
    old = context.user_data.get("profile_edit_old", {})
    old_name = old.get("full_name") or ""
    old_phone = old.get("phone") or ""

    # «-» означает «оставить текущее значение»
    new_name = context.user_data.get("profile_edit_new_name") or old_name
    new_phone_val = context.user_data.get("profile_edit_new_phone") or old_phone

    User.update_contacts(user_id, new_name, new_phone_val)
    logger.info(
        "Пользователь %s обновил профиль: %s -> %s, %s -> %s",
        user_id,
        old_name,
        new_name,
        old_phone,
        new_phone_val,
    )

    # Уведомление админу о смене данных
    telegram_id = update.effective_user.id
    admin_msg = (
        "\u270f\ufe0f Смена данных профиля\n\n"
        f"Клиент: {old_name or '—'} (ID {telegram_id})\n"
        f"Было: {old_name or '—'}, {old_phone or '—'}\n"
        f"Стало: {new_name or '—'}, {new_phone_val or '—'}"
    )
    await notify_admin(context, admin_msg)

    await update.message.reply_text("\u2705 Данные обновлены.")
    # Возвращаем экран профиля
    await show_profile(update, context)
    return ConversationHandler.END


async def profile_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена редактирования профиля (кнопка profile_cancel или /cancel)."""
    # Очищаем временные данные диалога
    for key in list(context.user_data):
        if key.startswith("profile_edit_"):
            context.user_data.pop(key, None)

    if update.callback_query:
        await safe_answer(update.callback_query)
        await update.callback_query.edit_message_text(
            "\u270f\ufe0f Изменение данных отменено."
        )
    else:
        await update.message.reply_text("\u270f\ufe0f Изменение данных отменено.")

    # Показываем профиль (или меню, если нет пользователя)
    user = User.get_by_telegram_id(update.effective_user.id)
    if user:
        await show_profile(update, context)
    else:
        await show_info_menu(update, context)
    return ConversationHandler.END


# Подменю "Направления"
# ----------------------------------------------------------------------
async def show_directions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает подменю со всеми направлениями."""
    query = update.callback_query
    await safe_answer(query)

    keyboard = [
        [InlineKeyboardButton("\U0001f525 Трайбл фьюжн", callback_data="tribal_info")],
        [InlineKeyboardButton("\U0001fa70 Восточный танец", callback_data="oriental_info")],
        [InlineKeyboardButton("\U0001f9d8 Йога", callback_data="yoga_info")],
        [InlineKeyboardButton("\U0001f483 Бачата (для пар)", callback_data="bachata_info")],
        [InlineKeyboardButton("\U0001f33f Цигун", callback_data="qigong_info")],
        [InlineKeyboardButton("\U0001f1ee\U0001f1f3 Индийский танец", callback_data="indian_info")],
        [InlineKeyboardButton("🇮🇳 Индийский танец", callback_data="indian_info")],
        [InlineKeyboardButton("🏛️ Исторический танец", callback_data="historical_info")],
        [InlineKeyboardButton("🧘 Пилатес", callback_data="pilates_info")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🧭 Направления студии Soul:\n\nВыберите направление, чтобы узнать подробнее:",
        reply_markup=reply_markup,
    )


# ----------------------------------------------------------------------
# Описания направлений
# ----------------------------------------------------------------------
async def show_tribal_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_tribal",
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Назад к направлениям", callback_data="directions"
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "\U0001f525 Трайбл фьюжн (Tribal Fusion)\n\n"
        "Новый виток развития восточного танца. Это медитативность, "
        "поиск себя и искусное владение телом. Техника сочетает элементы "
        "восточного, испанского, индийского и других танцев, предоставляя "
        "большую свободу для самовыражения.\n\n"
        "Подробнее: http://soul.by/index.php/tribal/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_oriental_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_oriental",
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Назад к направлениям", callback_data="directions"
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "\U0001fa70 Восточный танец (Беллиданс)\n\n"
        "Древнейшая танцевальная техника, которая помогает раскрыть "
        "женственность, мягкость и сексуальность. Учит находить баланс "
        "между напряжением и расслаблением, избавляет от последствий стресса.\n\n"
        "Подробнее: http://soul.by/index.php/vostocniy-tanec/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_yoga_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_yoga",
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Назад к направлениям", callback_data="directions"
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "\U0001f9d8 Йога\n\n"
        "Практика, направленная на оздоровление организма, поиск его "
        "скрытых возможностей и создание правильного умственного и "
        "эмоционального настроя. В студии преподают хатха-йогу, "
        "адаптированную к нуждам современного человека.\n\n"
        "Подробнее: http://soul.by/index.php/yoga-minsk/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_bachata_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_bachata",
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Назад к направлениям", callback_data="directions"
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "\U0001f483 Бачата (для пар)\n\n"
        "Бачата — это диалог тел, разговор без слов. В студии предлагают "
        "уникальный формат, где пары проходят весь путь обучения вместе, "
        "не меняя партнёров. Это идеальный способ укрепить отношения, "
        "раскрыть новый уровень доверия и гармонии, а также устроить "
        "необычное свидание. Обучение ведётся с нуля в комфортной и "
        "дружеской атмосфере.\n\n"
        "Подробнее: https://soul.by/bachata-minsk/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_qigong_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_qigong",
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Назад к направлениям", callback_data="directions"
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "\U0001f33f Цигун\n\n"
        "\u00abЦи\u00bb — поток жизненной энергии, \u00abГун\u00bb — работа. Это система "
        "плавных, контролируемых движений, управления дыханием и вниманием, "
        "выросшая из китайской народной медицины. Занятия цигун наполняют "
        "энергией, дарят здоровье и долголетие, а также способствуют "
        "спокойствию ума и внутреннему равновесию.\n\n"
        "Подробнее: https://soul.by/cigun-minsk/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_indian_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_indian",
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Назад к направлениям", callback_data="directions"
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "\U0001f1ee\U0001f1f3 Индийский танец\n\n"
        "Индийский танец — это не просто искусство, а духовная практика, "
        "воплощающая древнюю мудрость. Подобно йоге, он способен погрузить "
        "танцора в состояние транса, раскрывая внутреннюю сущность. "
        "В студии преподают как классический индийский танец, так и "
        "современный и яркий стиль \u00abБолливуд\u00bb, а также другие стили: "
        "Гарба, Дандия, Лавани и Бхангра.\n\n"
        "Подробнее: https://soul.by/indian-dance-minsk/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_historical_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_historical",
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Назад к направлениям", callback_data="directions"
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "\U0001f3db\ufe0f Исторический танец\n\n"
        "Это прекрасная возможность погрузиться в миры любимых книг и "
        "фильмов, узнать много нового об истории и культуре, завести "
        "интересные знакомства. Значительную часть исторических танцев "
        "можно танцевать без особенной физической подготовки, ведь они "
        "были придуманы для того, чтобы совершенно обычные люди могли "
        "весело и интересно провести вечер. Вас ожидает: изучение схем "
        "танцев из исторических сборников, освоение нескольких видов "
        "вальса, участие в балах и пикниках, основы бального этикета.\n\n"
        "Подробнее: https://soul.by/historical-dance/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_pilates_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_pilates",
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Назад к направлениям", callback_data="directions"
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "\U0001f9d8 Пилатес\n\n"
        "Пилатес — это уникальная система физических упражнений, которая "
        "направлена на оздоровление организма и восстановление естественного "
        "положения тела в пространстве. Это одна из наиболее щадящих форм "
        "воздействия на организм, разработанная для реабилитации людей "
        "после травм позвоночника, поэтому она не имеет практически "
        "никаких противопоказаний. Пилатес доступен людям любого уровня "
        "подготовки. Занятия включают работу с основными принципами "
        "пилатеса: концентрация, мышечный контроль, правильное дыхание, "
        "плавность движений, а также проработку глубинных мышц, "
        "служащих \u00abкаркасом\u00bb для позвоночника.\n\n"
        "Подробнее: https://soul.by/pilates/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


# ----------------------------------------------------------------------
# /start
# ----------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start — создаёт/возвращает пользователя и показывает меню
    в зависимости от статуса.
    """
    tg_user = update.effective_user
    utm_source = context.args[0] if context.args else "direct"

    telegram_id = tg_user.id
    username = tg_user.username

    # Создаём или получаем пользователя
    user = User.create_or_get(telegram_id, username)

    # Обновляем активность
    User.update_activity(user["id"])

    # Автозавершение заморозки: если клиент вернулся во время/после неё —
    # фиксируем факт и продлеваем expires_at абонемента
    check_and_complete_freeze(user["id"])

    # --- Приветствие ---
    welcome = f"\U0001f44b Привет, {tg_user.first_name}!"
    if utm_source == "bachata":
        welcome += "\n\nРады видеть вас на странице Бачаты! \U0001f483"
    else:
        welcome += "\n\nДобро пожаловать в нашу студию танцев и йоги! \U0001f9d8"

    # Меню по статусу
    if user["status"] == "new" or not user["consent_given"]:
        await show_info_menu(update, context, text=welcome)
    else:
        await show_menu_by_role(update, context, user, text=welcome)


# ----------------------------------------------------------------------
# /ask — вопрос к RAG-агенту
# ----------------------------------------------------------------------
async def ask_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /ask <вопрос> — возвращает ответ RAG-агента."""
    question = " ".join(context.args)

    if not question:
        await update.message.reply_text(
            "💡 Как задать вопрос:\n\n"
            "Просто напишите: /ask <ваш вопрос>\n\n"
            "Например:\n/ask что такое бачата?\n/ask сколько стоят абонементы?"
        )
        return

    # Ответ с индикатором "печатает..."
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    # RAG-агент: поиск по базе знаний + генерация ответа LLM
    answer = await asyncio.to_thread(ask, question, update.effective_user.id)
    await update.message.reply_text(answer)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Если пользователь написал просто текст — тоже отвечаем через RAG."""
    # Игнорируем сообщения внутри диалога записи (их ловит ConversationHandler)
    if context.user_data.get("booking_active"):
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    answer = await asyncio.to_thread(ask, update.message.text, update.effective_user.id)
    await update.message.reply_text(answer)


# ----------------------------------------------------------------------
# /cancel — отмена любого диалога
# ----------------------------------------------------------------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет диалог и возвращает информационное меню."""
    context.user_data["booking_active"] = False
    await show_info_menu(
        update, context, text="\u274c Регистрация отменена. Возвращаюсь в меню."
    )
    return ConversationHandler.END


# ----------------------------------------------------------------------
# ConversationHandler: запись на занятие
# ----------------------------------------------------------------------
async def book_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Шаг 1 регистрации — показать согласие на ПДн.
    """
    context.user_data["booking_active"] = True
    await safe_answer(update.callback_query)

    text = (
        "\u270d\ufe0f Регистрация в студии Soul\n\n"
        "Перед регистрацией нам нужно ваше согласие на обработку "
        "персональных данных:\n\n"
        f"{CONSENT_TEXT}\n\n"
        'Нажмите "\u2705 Согласен", чтобы продолжить, '
        "или /cancel чтобы отменить."
    )
    keyboard = [
        [InlineKeyboardButton("\u2705 Согласен", callback_data="consent_agree")],
    ]
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONSENT


async def book_consent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик согласия — после нажатия кнопки.
    Вызывается внутри ConversationHandler.
    """
    query = update.callback_query
    await safe_answer(query)

    telegram_id = update.effective_user.id
    user = User.get_by_telegram_id(telegram_id)
    if user:
        User.set_consent(user["id"], CONSENT_TEXT_VERSION)
        context.user_data["user_id"] = user["id"]

    await query.edit_message_text(
        "\u2705 Спасибо! Согласие зафиксировано.\n\n"
        "Шаг 1 из 2. Как вас зовут? (напишите имя и фамилию)\n\n"
        "Или нажмите /cancel чтобы отменить регистрацию."
    )
    return FULL_NAME


async def book_full_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2 — сохраняем имя, запрашиваем телефон."""
    full_name = update.message.text.strip()
    if not full_name:
        await update.message.reply_text("Пожалуйста, напишите ваше имя.")
        return FULL_NAME

    context.user_data["full_name"] = full_name
    await update.message.reply_text(
        f"Отлично, {full_name}!\n\n"
        "Шаг 2 из 2. Укажите ваш номер телефона, чтобы мы могли с вами связаться:\n\n"
        "Или нажмите /cancel чтобы отменить регистрацию."
    )
    return PHONE


async def book_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Финал — сохраняем телефон, обновляем статус, уведомляем админа."""
    phone = update.message.text.strip()
    if not phone:
        await update.message.reply_text("Пожалуйста, напишите ваш номер телефона.")
        return PHONE

    full_name = context.user_data.get("full_name", "")
    telegram_id = update.effective_user.id

    # Получаем или создаём пользователя
    user = User.get_by_telegram_id(telegram_id)
    if not user:
        user = User.create_or_get(telegram_id, update.effective_user.username)

    user_id = user["id"]

    # Сохраняем контакты
    User.update_contacts(user_id, full_name, phone)
    # Меняем статус на active
    User.set_status(user_id, "active")
    # Если согласие ещё не дано — ставим (на случай, если bypass)
    if not user.get("consent_given"):
        User.set_consent(user_id, CONSENT_TEXT_VERSION)

    # Уведомление администратору
    if ADMIN_CHAT_ID:
        admin_message = (
            f"\U0001f389 Новый клиент зарегистрирован!\n\n"
            f"Имя: {full_name}\n"
            f"Телефон: {phone}\n"
            f"Telegram ID: {telegram_id}\n"
            f"Username: @{update.effective_user.username or '—'}"
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=admin_message,
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление админу: {e}")

    # Показываем подтверждение и меню ученика
    await update.message.reply_text(
        f"\u2705 Регистрация завершена!\n\n"
        f"Имя: {full_name}\n"
        f"Телефон: {phone}\n\n"
        "Добро пожаловать в студию Soul!"
    )
    context.user_data["booking_active"] = False
    # Перезагружаем пользователя из БД (там уже active)
    user = User.get_by_telegram_id(telegram_id)
    await show_menu_by_role(update, context, user)
    return ConversationHandler.END


# ----------------------------------------------------------------------
# Админ-панель
# ----------------------------------------------------------------------


async def _require_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[dict]:
    """
    Проверяет, что пользователь — admin или super_admin.
    Возвращает dict пользователя или None (и показывает alert).
    """
    query = update.callback_query
    user = User.get_by_telegram_id(update.effective_user.id)
    if not user:
        await safe_answer(query, "Не зарегистрирован", show_alert=True)
        return None
    roles = UserRole.get_active_roles(user["id"])
    if "admin" not in roles and "super_admin" not in roles:
        await safe_answer(query, "Нет доступа", show_alert=True)
        return None
    return user


async def _require_teacher(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[dict]:
    """
    Проверяет, что пользователь — teacher (или admin/super_admin).
    Возвращает dict пользователя или None (и показывает alert).
    """
    query = update.callback_query
    user = User.get_by_telegram_id(update.effective_user.id)
    if not user:
        await safe_answer(query, "Не зарегистрирован", show_alert=True)
        return None
    roles = UserRole.get_active_roles(user["id"])
    if (
        "teacher" not in roles
        and "admin" not in roles
        and "super_admin" not in roles
    ):
        await safe_answer(query, "Нет доступа", show_alert=True)
        return None
    return user


async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню админ-панели."""
    user = await _require_admin(update, context)
    if not user:
        return

    keyboard = [
        [InlineKeyboardButton("\U0001f465 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton("\U0001f3ab Роли", callback_data="admin_roles")],
        [InlineKeyboardButton("\U0001f3e0 Главное меню", callback_data="back_to_main")],
    ]
    text = "\U0001f527 Панель администратора"
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сводка по пользователям."""
    user = await _require_admin(update, context)
    if not user:
        return

    counts = User.count_by_status()
    text = (
        "\U0001f465 Пользователи\n\n"
        f"\U0001f195 Новые (без регистрации): {counts.get('new', 0)}\n"
        f"\u2705 Активные: {counts.get('active', 0)}\n"
        f"\u23f3 Истёкшие: {counts.get('expired', 0)}\n"
        f"\U0001f4e6 Всего: {counts.get('total', 0)}"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f195 Новые", callback_data="admin_users_new"
            ),
            InlineKeyboardButton(
                "\u2705 Активные", callback_data="admin_users_active"
            ),
        ],
        [
            InlineKeyboardButton(
                "\U0001f4e6 Все", callback_data="admin_users_all"
            )
        ],
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_panel")],
    ]
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_admin_users_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE, status_filter: str = None
):
    """Список пользователей по фильтру статуса."""
    user = await _require_admin(update, context)
    if not user:
        return

    status_labels = {
        "new": "\U0001f195 Новые (зарегистрировались в боте, но не прошли регистрацию)",
        "active": "\u2705 Активные (прошли регистрацию)",
    }
    if status_filter:
        title = status_labels.get(status_filter, f"Статус: {status_filter}")
    else:
        title = "\U0001f4e6 Все пользователи"

    users = User.list_by_status(status_filter=status_filter, limit=20)

    if not users:
        text = f"{title}\n\nНет пользователей."
    else:
        lines = [title, ""]
        for i, u in enumerate(users, 1):
            username = f"@{u['username']}" if u.get("username") else "—"
            reg_date = u["registered_at"]
            if reg_date:
                reg_date = reg_date.strftime("%d.%m.%Y") if hasattr(reg_date, "strftime") else str(reg_date)[:10]
            else:
                reg_date = "—"
            lines.append(
                f"{i}. {u['full_name'] or '—'} — {username}\n"
                f"   Статус: {u['status']}, зашёл: {reg_date}"
            )
        text = "\n".join(lines)

    # Кнопка «Карточка» для каждого пользователя
    keyboard = []
    for u in users[:20]:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"\U0001f464 {u['full_name'] or u['username'] or u['id']}",
                    callback_data=f"admin_user_{u['id']}",
                )
            ]
        )

    # Кнопка назад — к сводке пользователей
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_users")]
    )
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_admin_user_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
):
    """Карточка пользователя."""
    admin = await _require_admin(update, context)
    if not admin:
        return

    u = User.get_by_id(user_id)
    if not u:
        await safe_answer(
            update.callback_query, "Пользователь не найден", show_alert=True
        )
        return

    roles = UserRole.get_active_roles(user_id)
    roles_str = ", ".join(roles) if roles else "—"
    consent = (
        f"да (v{u.get('consent_text_version', '—')})"
        if u.get("consent_given")
        else "нет"
    )
    reg_date = u.get("registered_at")
    if reg_date:
        reg_date = reg_date.strftime("%d.%m.%Y") if hasattr(reg_date, "strftime") else str(reg_date)[:10]
    else:
        reg_date = "—"

    text = (
        f"\U0001f464 {u['full_name'] or '—'}\n"
        f"ID в БД: {u['id']}\n"
        f"Telegram ID: {u['telegram_id']}\n"
        f"Username: @{u['username'] or '—'}\n"
        f"Телефон: {u['phone'] or '—'}\n"
        f"Статус: {u['status']}\n"
        f"Роли: {roles_str}\n"
        f"Согласие на ПДн: {consent}\n"
        f"Зарегистрирован: {reg_date}"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f3ab Роли", callback_data=f"admin_user_roles_{user_id}"
            ),
            InlineKeyboardButton(
                "\U0001f4b0 Абонементы", callback_data=f"admin_user_subs_{user_id}"
            ),
        ],
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_users")],
    ]
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_admin_roles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список последних пользователей для управления ролями."""
    admin = await _require_admin(update, context)
    if not admin:
        return

    users = User.list_by_status(limit=20)

    if not users:
        text = "\U0001f3ab Управление ролями\n\nНет пользователей."
    else:
        lines = ["\U0001f3ab Управление ролями", "Последние 20 пользователей:", ""]
        for u in users:
            username = f"@{u['username']}" if u.get("username") else "—"
            lines.append(
                f"[{u['id']}] {u['full_name'] or '—'} — {username}"
            )
        text = "\n".join(lines)

    keyboard = []
    for u in users[:20]:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"\U0001f464 {u['full_name'] or u['username'] or u['id']}",
                    callback_data=f"admin_user_roles_{u['id']}",
                )
            ]
        )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_panel")]
    )
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_admin_user_roles(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
):
    """Управление ролями конкретного пользователя."""
    admin = await _require_admin(update, context)
    if not admin:
        return

    u = User.get_by_id(user_id)
    if not u:
        await safe_answer(
            update.callback_query, "Пользователь не найден", show_alert=True
        )
        return

    active_roles = UserRole.get_active_roles(user_id)
    all_role_names = ["student", "teacher", "admin"]

    # Строим список текущих ролей
    current_lines = []
    for role in all_role_names:
        if role in active_roles:
            current_lines.append(f"\u2705 {role}")
        else:
            current_lines.append(f"\u274c {role}")

    text = (
        f"\U0001f3ab Роли: {u['full_name'] or u['username'] or u['id']}\n\n"
        "Текущие:\n" + "\n".join(current_lines)
    )

    keyboard = []
    # Кнопки назначения ролей (кроме student — выдаётся автоматически)
    for role in ["teacher", "admin"]:
        if role not in active_roles:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"\u2795 {role}",
                        callback_data=f"admin_role_grant_{role}_{user_id}",
                    )
                ]
            )
        else:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"\u274c Снять {role}",
                        callback_data=f"admin_role_revoke_{role}_{user_id}",
                    )
                ]
            )

    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_roles")]
    )
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_admin_role_grant(
    update: Update, context: ContextTypes.DEFAULT_TYPE, role: str, user_id: int
):
    """Выдать роль пользователю."""
    admin = await _require_admin(update, context)
    if not admin:
        return

    UserRole.grant(user_id, role, granted_by=admin["id"])
    logger.info(
        "Админ %s выдал роль %s пользователю %s", admin["id"], role, user_id
    )
    await show_admin_user_roles(update, context, user_id)


async def show_admin_role_revoke(
    update: Update, context: ContextTypes.DEFAULT_TYPE, role: str, user_id: int
):
    """Снять роль у пользователя."""
    admin = await _require_admin(update, context)
    if not admin:
        return

    UserRole.revoke(user_id, role)
    logger.info(
        "Админ %s снял роль %s у пользователя %s", admin["id"], role, user_id
    )
    await show_admin_user_roles(update, context, user_id)


# ----------------------------------------------------------------------
# Админка: абонементы пользователей
# ----------------------------------------------------------------------

# Человекочитаемые подписи статусов абонемента для админки
ADMIN_SUB_STATUS_LABELS = {
    "active": "\U0001f7e2 Активный",
    "frozen": "\U0001f9ca Заморожен",
    "expired": "\U0001f534 Сгорел",
    "depleted": "\U0001f534 Завершён",
    "cancelled": "\U0001f534 Отменён",
}


async def show_admin_user_subs(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    notice: str = None,
):
    """Экран «Абонементы пользователя» (только admin/super_admin)."""
    admin = await _require_admin(update, context)
    if not admin:
        return

    query = update.callback_query
    user = User.get_by_id(user_id)
    if not user:
        await safe_answer(query, "Пользователь не найден", show_alert=True)
        return

    subs = Subscription.get_for_user(user_id)
    types = {t["id"]: t for t in SubscriptionType.get_all()}

    lines = [f"\U0001f4b0 Абонементы: {user['full_name'] or user['username'] or user_id}"]
    keyboard = []

    # Активный абонемент (для него — кнопка управления)
    active = [s for s in subs if s["status"] == "active"]
    if active:
        s = active[0]
        sub_type = types.get(s.get("type_id")) or {}
        type_name = sub_type.get("name") or "—"
        hours_left = s.get("hours_left")
        hours_str = f"{hours_left}ч" if hours_left is not None else "безлимит"
        lines.append("")
        lines.append("\U0001f7e2 Активный:")
        lines.append(
            f"#{s['id']} · {type_name} · Осталось: {hours_str} · "
            f"До: {_format_date(s.get('expires_at'))}"
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\u270f\ufe0f Управление абонементом",
                    callback_data=f"admin_sub_manage_{s['id']}",
                )
            ]
        )
    else:
        lines.append("")
        lines.append("\U0001f7e2 Активный: нет.")

    # Архив (неактивные абонементы)
    archived = [s for s in subs if s["status"] != "active"]
    if archived:
        lines.append("")
        lines.append("\U0001f534 Архив:")
        for s in archived:
            sub_type = types.get(s.get("type_id")) or {}
            type_name = sub_type.get("name") or "—"
            label = ADMIN_SUB_STATUS_LABELS.get(s["status"], s["status"])
            lines.append(
                f"#{s['id']} · {type_name} · {label} {_format_date(s.get('expires_at'))}"
            )

    text = "\n".join(lines)
    if notice:
        text = f"{notice}\n\n{text}"

    keyboard.append(
        [
            InlineKeyboardButton(
                "\u2795 Выдать новый абонемент",
                callback_data=f"admin_sub_create_{user_id}",
            )
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                "\U0001f519 К карточке юзера",
                callback_data=f"admin_user_{user_id}",
            )
        ]
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_sub_create_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
):
    """Выдача нового абонемента — список тарифов на выбор."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    user = User.get_by_id(user_id)
    if not user:
        await safe_answer(query, "Пользователь не найден", show_alert=True)
        return

    types = SubscriptionType.get_active()
    if not types:
        await query.edit_message_text("Нет доступных тарифов.")
        return

    text = (
        f"\U0001f4b0 Выдача абонемента: "
        f"{user['full_name'] or user['username'] or user_id}\n\n"
        "Выберите тариф:"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                f"{t['name']} ({t['price_byn']} BYN)",
                callback_data=f"admin_sub_type_{user_id}_{t['id']}",
            )
        ]
        for t in types
    ]
    keyboard.append(
        [
            InlineKeyboardButton(
                "\U0001f519 Назад", callback_data=f"admin_user_subs_{user_id}"
            )
        ]
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_sub_type_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, type_id: int
):
    """Создание абонемента выбранного типа (шаг после выбора тарифа)."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    user = User.get_by_id(user_id)
    sub_type = SubscriptionType.get_by_id(type_id)
    if not user or not sub_type:
        await safe_answer(query, "Данные не найдены", show_alert=True)
        return

    now = datetime.now()
    # NULL допустим только у безлимитного тарифа; иначе — ошибка данных
    hours = sub_type.get("hours_count")
    if hours is not None:
        hours = float(hours)
    elif "безлимит" not in (sub_type.get("name") or "").lower():
        await safe_answer(
            query, "Ошибка: у тарифа не указано количество часов", show_alert=True
        )
        return
    expires_at = now + timedelta(days=sub_type["duration_days"] or 30)

    Subscription.create(
        user_id=user_id,
        type_id=type_id,
        purchased_at=now,
        started_at=now,
        expires_at=expires_at,
        hours_total=hours,
        hours_left=hours,
        status="active",
    )
    logger.info(
        "Админ %s выдал абонемент %s (type_id=%s) клиенту %s",
        admin["id"],
        sub_type["name"],
        type_id,
        user_id,
    )

    # Возвращаемся к списку абонементов с уведомлением о выдаче
    notice = (
        f"\u2705 Абонемент {sub_type['name']} выдан клиенту "
        f"{user['full_name'] or user_id}"
    )
    await show_admin_user_subs(update, context, user_id, notice=notice)


async def show_admin_sub_manage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    sub_id: int,
    notice: str = None,
):
    """Экран управления конкретным абонементом (dual-mode: кнопка/сообщение)."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    sub = Subscription.get_by_id(sub_id)
    if not sub:
        if query:
            await safe_answer(query, "Абонемент не найден", show_alert=True)
        else:
            await update.message.reply_text("Абонемент не найден.")
        return

    sub_type = SubscriptionType.get_by_id(sub["type_id"]) if sub.get("type_id") else None
    type_name = sub_type["name"] if sub_type else "Абонемент"

    hours_left = sub.get("hours_left")
    if hours_left is None:
        # NULL допустим только у безлимитного тарифа; иначе — ошибка данных
        if sub_type and "безлимит" in (sub_type.get("name") or "").lower():
            hours_line = "Осталось: Безлимит"
        else:
            hours_line = "Осталось: 0ч (ошибка данных)"
    else:
        hours_total = sub.get("hours_total")
        hours_line = f"Осталось: {hours_left}ч из {hours_total}ч"

    text = (
        f"\u2699\ufe0f Управление: {type_name}\n"
        f"{hours_line}\n"
        f"Действует до: {_format_date(sub.get('expires_at'))}"
    )
    if notice:
        text = f"{notice}\n\n{text}"

    keyboard = [
        [
            InlineKeyboardButton(
                "\u2795 Добавить 1ч", callback_data=f"admin_sub_add_hours_{sub_id}"
            ),
            InlineKeyboardButton(
                "\u2796 Списать 1ч", callback_data=f"admin_sub_sub_hours_{sub_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                "\U0001f4c5 Изменить дату окончания",
                callback_data=f"admin_sub_expires_{sub_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "\u2705 Отметить посещение",
                callback_data=f"admin_sub_visit_{sub_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "\u274c Отменить абонемент",
                callback_data=f"admin_sub_cancel_{sub_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Назад",
                callback_data=f"admin_user_subs_{sub['user_id']}",
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if query:
        await query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def handle_sub_add_hours(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int
):
    """Быстрое добавление 1 часа к абонементу."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    if Subscription.update_hours(sub_id, 1.0):
        logger.info("Админ %s добавил 1ч абонементу %s", admin["id"], sub_id)
    await show_admin_sub_manage(update, context, sub_id)


async def handle_sub_sub_hours(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int
):
    """Быстрое списание 1 часа (не ниже 0)."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    if Subscription.update_hours(sub_id, -1.0):
        logger.info("Админ %s списал 1ч с абонемента %s", admin["id"], sub_id)
    await show_admin_sub_manage(update, context, sub_id)


async def admin_sub_expires_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1 — запрос новой даты окончания (ConversationHandler)."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END
    query = update.callback_query

    # ConversationHandler вызывает обработчик только с (update, context) —
    # ID абонемента достаём из callback_data
    try:
        sub_id = int(query.data.replace("admin_sub_expires_", ""))
    except (ValueError, AttributeError):
        await query.edit_message_text("Ошибка: неверный ID абонемента.")
        return ConversationHandler.END

    sub = Subscription.get_by_id(sub_id)
    if not sub:
        await safe_answer(query, "Абонемент не найден", show_alert=True)
        return ConversationHandler.END

    context.user_data["admin_sub_id"] = sub_id
    text = (
        f"\U0001f4c5 Текущая дата: {_format_date(sub.get('expires_at'))}.\n\n"
        "Введите новую дату в формате ДД.ММ.ГГГГ:"
    )
    keyboard = [
        [InlineKeyboardButton("\U0001f519 Отмена", callback_data="admin_sub_expires_cancel")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_SUB_EXPIRES_DATE


async def admin_sub_expires_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2 — валидация даты и обновление expires_at."""
    text = update.message.text.strip()
    try:
        new_expires = datetime.strptime(text, "%d.%m.%Y")
    except ValueError:
        await update.message.reply_text(
            "\u26a0\ufe0f Неверный формат. Введите дату как ДД.ММ.ГГГГ "
            "(например, 25.12.2026):"
        )
        return ADMIN_SUB_EXPIRES_DATE

    if new_expires <= datetime.now():
        await update.message.reply_text(
            "\u26a0\ufe0f Дата должна быть в будущем. Введите ещё раз:"
        )
        return ADMIN_SUB_EXPIRES_DATE

    sub_id = context.user_data.get("admin_sub_id")
    context.user_data.pop("admin_sub_id", None)
    if not sub_id:
        await update.message.reply_text("Диалог устарел. Откройте управление заново.")
        return ConversationHandler.END

    sub = Subscription.get_by_id(sub_id)
    if not sub:
        await update.message.reply_text("Абонемент не найден.")
        return ConversationHandler.END

    Subscription.update_expires(sub_id, new_expires)
    logger.info(
        "Админ %s изменил дату окончания абонемента %s на %s",
        update.effective_user.id,
        sub_id,
        _format_date(new_expires),
    )

    # Опционально — уведомляем клиента о продлении
    client = User.get_by_id(sub["user_id"])
    if client and client.get("telegram_id"):
        try:
            await context.bot.send_message(
                chat_id=client["telegram_id"],
                text=f"\u2705 Ваш абонемент продлён до {_format_date(new_expires)}.",
            )
        except Exception as e:
            logger.error(
                "Не удалось уведомить клиента %s: %s", client["telegram_id"], e
            )

    await update.message.reply_text(
        f"\u2705 Дата окончания обновлена: {_format_date(new_expires)}."
    )
    # Возвращаем экран управления абонементом
    await show_admin_sub_manage(update, context, sub_id)
    return ConversationHandler.END


async def admin_sub_expires_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена смены даты (кнопка или /cancel) — возврат к экрану управления."""
    sub_id = context.user_data.get("admin_sub_id")
    context.user_data.pop("admin_sub_id", None)

    if update.callback_query:
        await safe_answer(update.callback_query)
        await update.callback_query.edit_message_text(
            "\U0001f4c5 Изменение даты отменено."
        )
    else:
        await update.message.reply_text("\U0001f4c5 Изменение даты отменено.")

    if sub_id:
        await show_admin_sub_manage(update, context, sub_id)
    return ConversationHandler.END


async def admin_sub_visit_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int
):
    """Отметка посещения — запрос количества часов для списания."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    sub = Subscription.get_by_id(sub_id)
    if not sub:
        await safe_answer(query, "Абонемент не найден", show_alert=True)
        return

    text = "\u2705 Отметка посещения\n\nСколько часов списать?"
    keyboard = [
        [
            InlineKeyboardButton(
                "1 ч", callback_data=f"admin_sub_visit_hours_{sub_id}_1"
            ),
            InlineKeyboardButton(
                "1.5 ч", callback_data=f"admin_sub_visit_hours_{sub_id}_1.5"
            ),
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Назад", callback_data=f"admin_sub_manage_{sub_id}"
            )
        ],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_sub_visit_hours_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int, hours: float
):
    """Списание часов и создание записи посещения."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    sub = Subscription.get_by_id(sub_id)
    if not sub:
        await safe_answer(query, "Абонемент не найден", show_alert=True)
        return

    # Если у клиента активная заморозка и он вернулся — завершаем её
    check_and_complete_freeze(sub["user_id"])

    if not Subscription.update_hours(sub_id, -hours):
        await safe_answer(query, "Не удалось списать часы", show_alert=True)
        return

    # Ручная отметка посещения админом (без события из EM)
    Visit.create(
        user_id=sub["user_id"],
        em_event_id=0,
        subscription_id=sub_id,
        marked_at=datetime.now(),
        marked_by=admin["id"],
        method="manual_admin",
        visit_type="subscription",
        status="attended",
        hours_charged=hours,
    )
    logger.info(
        "Админ %s отметил посещение абонементу %s: %sч",
        admin["id"],
        sub_id,
        hours,
    )

    remaining = Subscription.get_by_id(sub_id).get("hours_left")
    remaining_str = f"{remaining}ч" if remaining is not None else "безлимит"
    notice = f"\u2705 Посещение отмечено. Списано {hours}ч. Осталось {remaining_str}"
    await show_admin_sub_manage(update, context, sub_id, notice=notice)


async def admin_sub_cancel_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int
):
    """Подтверждение отмены абонемента."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    sub = Subscription.get_by_id(sub_id)
    if not sub:
        await safe_answer(query, "Абонемент не найден", show_alert=True)
        return

    hours_left = sub.get("hours_left")
    left_str = f"{hours_left}ч" if hours_left is not None else "безлимит"
    text = f"\u274c Отменить абонемент?\n\nОстаток {left_str} сгорит."
    keyboard = [
        [
            InlineKeyboardButton(
                "\u2705 Да, отменить",
                callback_data=f"admin_sub_cancel_yes_{sub_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Нет", callback_data=f"admin_sub_manage_{sub_id}"
            )
        ],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_sub_cancel_yes_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int
):
    """Фактическая отмена абонемента."""
    admin = await _require_admin(update, context)
    if not admin:
        return

    sub = Subscription.get_by_id(sub_id)
    if not sub:
        await safe_answer(update.callback_query, "Абонемент не найден", show_alert=True)
        return

    Subscription.set_status(sub_id, "cancelled")
    logger.info("Админ %s отменил абонемент %s", admin["id"], sub_id)
    # Возвращаемся к списку абонементов пользователя
    await show_admin_user_subs(update, context, sub["user_id"])


# ----------------------------------------------------------------------
# Панель преподавателя
# ----------------------------------------------------------------------
async def show_teacher_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню панели преподавателя."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return
    query = update.callback_query

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Мои занятия сегодня", callback_data="teacher_today"
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f4c5 Расписание на неделю", callback_data="teacher_week"
            )
        ],
        [InlineKeyboardButton("\U0001f519 Главное меню", callback_data="back_to_main")],
    ]
    await query.edit_message_text(
        "\U0001f9d1\u200d\U0001f3eb Панель преподавателя",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_teacher_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Слоты преподавателя на текущий день недели."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return
    query = update.callback_query

    # date.isoweekday(): 1=Пн ... 7=Вс — совпадает с форматом weekdays в БД
    weekday = date.today().isoweekday()

    # Слоты преподавателя, отфильтрованные по дню недели
    today_slots = []
    for slot in Group.get_by_teacher(teacher["id"]):
        days = [
            d.strip()
            for d in (slot.get("weekdays") or "").split(",")
            if d.strip()
        ]
        if str(weekday) in days:
            today_slots.append(slot)

    if not today_slots:
        text = "\U0001f4c5 Мои занятия сегодня\n\nУ вас нет занятий сегодня."
        keyboard = [
            [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_panel")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    lines = ["\U0001f4c5 Мои занятия сегодня", ""]
    keyboard = []
    for slot in today_slots:
        time_str = _format_time(slot.get("time_start"))
        name = slot.get("name") or slot.get("category_slug") or "Занятие"
        location = slot.get("location_id") or "—"

        # Пока слоты не связаны с событиями EM — количество записей недоступно
        em_event_id = slot.get("em_event_id")
        if em_event_id:
            count = Booking.count_confirmed_for_event(em_event_id)
            count_str = f"{count} чел."
        else:
            count_str = "—"

        lines.append(f"\U0001f555 {time_str} — {name} (Зал {location})")
        lines.append(f"Записано: {count_str}")
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f465 Список",
                    callback_data=f"teacher_slot_{slot['id']}",
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_panel")]
    )
    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_teacher_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Расписание преподавателя на неделю — заглушка."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return
    query = update.callback_query

    text = "\U0001f4c5 Расписание на неделю будет доступно позже."
    keyboard = [
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_panel")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def show_teacher_slot(
    update: Update, context: ContextTypes.DEFAULT_TYPE, slot_id: int
):
    """Список записавшихся на слот (заглушка до интеграции с EM)."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return
    query = update.callback_query

    slot = Group.get_by_id(slot_id)
    if not slot:
        await safe_answer(query, "Слот не найден", show_alert=True)
        return

    # Если у слота появится привязка к событию EM — покажем реальные записи
    em_event_id = slot.get("em_event_id")
    if em_event_id:
        bookings = Booking.get_by_event(em_event_id)
        if bookings:
            lines = ["\U0001f465 Записи на занятие", ""]
            for b in bookings:
                who = b.get("full_name") or b.get("username") or f"ID {b.get('user_id')}"
                lines.append(f"• {who}")
            text = "\n".join(lines)
            keyboard = [
                [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_today")]
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            return

    text = (
        "\U0001f465 Записи на занятие\n\n"
        "Интеграция со списком записей появится позже. Пока вы можете "
        "отметить посещение вручную."
    )
    keyboard = [
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_today")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ----------------------------------------------------------------------
# Обработчик инлайн-кнопок (все, кроме book, который перехватывается
# ConversationHandler)
# ----------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)

    data = query.data

    # Логируем все callback_data для отладки
    logger.info("Callback: %s", data)

    # --- Согласие на обработку ПДн ---
    if data == "consent_agree":
        tg_user = update.effective_user
        user = User.get_by_telegram_id(tg_user.id)
        if user:
            User.set_consent(user["id"], CONSENT_TEXT_VERSION)
            logger.info(
                "Пользователь %s дал согласие на ПДн (версия %s)",
                user["id"],
                CONSENT_TEXT_VERSION,
            )
            await show_menu_by_role(
                update,
                context,
                user,
                text="\u2705 Спасибо! Согласие зафиксировано. Выберите действие:",
            )
        else:
            await query.edit_message_text(
                "\u26a0\ufe0f Что-то пошло не так. Нажмите /start, чтобы продолжить."
            )
        return

    # --- Расписание: плоский показ на выбранный день ---
    if data == "schedule":
        await show_schedule_day(update, context, None)
        return

    # --- Расписание: переключение дня по schedule_day_YYYY-MM-DD[#tag] ---
    if data.startswith("schedule_day_"):
        raw = data[len("schedule_day_") :]
        if "#" in raw:
            date_str, tag_slug = raw.split("#", 1)
        else:
            date_str, tag_slug = raw, None
        await show_schedule_day(update, context, date_str, tag_slug)
        return

    # --- Расписание по направлению: direction_schedule_{direction_slug} ---
    if data.startswith("direction_schedule_"):
        direction_slug = data.replace("direction_schedule_", "")
        tag_slug = DIRECTION_TO_TAG.get(direction_slug)
        if not tag_slug:
            await query.edit_message_text(
                "Расписание по этому направлению пока недоступно."
            )
            return
        await show_schedule_day(update, context, None, tag_slug=tag_slug)
        return

    # --- Запись на событие (заглушка) ---
    if data.startswith("book_event_"):
        try:
            event_id = int(data[len("book_event_") :])
        except ValueError:
            event_id = 0
        await book_event_stub(update, context, event_id)
        return

    # --- Подменю "Направления" ---
    if data == "directions":
        await show_directions_menu(update, context)
        return

    # --- Описания направлений ---
    if data == "tribal_info":
        await show_tribal_info(update, context)
        return
    if data == "oriental_info":
        await show_oriental_info(update, context)
        return
    if data == "yoga_info":
        await show_yoga_info(update, context)
        return
    if data == "bachata_info":
        await show_bachata_info(update, context)
        return
    if data == "qigong_info":
        await show_qigong_info(update, context)
        return
    if data == "indian_info":
        await show_indian_info(update, context)
        return
    if data == "historical_info":
        await show_historical_info(update, context)
        return
    if data == "pilates_info":
        await show_pilates_info(update, context)
        return

    # --- Профиль ---
    if data == "profile":
        await show_profile(update, context)
        return
    if data == "confirm_delete":
        await confirm_delete_data(update, context)
        return
    if data == "profile_delete_yes":
        await profile_delete_confirm(update, context)
        return

    # --- Возврат в меню ---
    if data == "back_to_main":
        telegram_id = update.effective_user.id
        user = User.get_by_telegram_id(telegram_id)
        if user:
            await show_menu_by_role(update, context, user)
        else:
            await show_info_menu(update, context)
        return

    # --- Абонемент ---
    if data == "my_subscription":
        await show_my_subscription(update, context)
        return

    if data == "sub_history":
        await show_subscription_history(update, context)
        return

    if data == "sub_freeze":
        await show_sub_freeze(update, context)
        return

    # Клиент: выбор причины заморозки
    if data.startswith("freeze_reason_"):
        reason_key = data[len("freeze_reason_"):]
        if reason_key in FREEZE_REASONS:
            await show_sub_freeze_confirm(update, context, reason_key)
            return

    # Клиент: подтверждение заявки на заморозку
    if data == "freeze_confirm":
        await submit_freeze(update, context)
        return

    # Админ: одобрение/отклонение заявки (из чата администратора)
    if data.startswith("freeze_approve_"):
        fid = _extract_int(data, "freeze_approve_")
        if fid is not None:
            await handle_freeze_approve(update, context, fid)
            return

    if data.startswith("freeze_reject_"):
        fid = _extract_int(data, "freeze_reject_")
        if fid is not None:
            await handle_freeze_reject(update, context, fid)
            return

    # --- Остальные пункты ---
    if data == "sub_buy_offline":
        await show_sub_buy_offline(update, context)
        return

    if data == "sub_buy_online":
        await show_sub_buy_online(update, context)
        return

    if data == "sub_freeze_cancel":
        await show_sub_freeze_cancel(update, context)
        return

    # --- Админ-панель ---
    if data == "admin_panel":
        await show_admin_panel(update, context)
        return

    if data == "admin_users":
        await show_admin_users(update, context)
        return

    if data == "admin_users_new":
        await show_admin_users_list(update, context, status_filter="new")
        return

    if data == "admin_users_active":
        await show_admin_users_list(update, context, status_filter="active")
        return

    if data == "admin_users_all":
        await show_admin_users_list(update, context)
        return

    if data == "admin_roles":
        await show_admin_roles(update, context)
        return

    # Управление ролями пользователя — проверяем ДО admin_user_
    if data.startswith("admin_user_roles_"):
        try:
            uid = int(data[len("admin_user_roles_"):])
            await show_admin_user_roles(update, context, uid)
            return
        except (ValueError, IndexError):
            pass

    # Абонементы пользователя — проверяем ДО admin_user_
    if data.startswith("admin_user_subs_"):
        uid = _extract_int(data, "admin_user_subs_")
        if uid is not None:
            await show_admin_user_subs(update, context, uid)
            return

    # Карточка пользователя
    if data.startswith("admin_user_"):
        try:
            uid = int(data[len("admin_user_"):])
            await show_admin_user_card(update, context, uid)
            return
        except ValueError:
            pass

    # Выдать роль
    if data.startswith("admin_role_grant_"):
        # Формат: admin_role_grant_{role}_{user_id}
        parts = data.split("_")
        # parts = ['admin', 'role', 'grant', 'teacher', '42']
        if len(parts) >= 5:
            role = parts[3]
            try:
                uid = int(parts[4])
                await show_admin_role_grant(update, context, role, uid)
                return
            except ValueError:
                pass

    # Снять роль
    if data.startswith("admin_role_revoke_"):
        parts = data.split("_")
        if len(parts) >= 5:
            role = parts[3]
            try:
                uid = int(parts[4])
                await show_admin_role_revoke(update, context, role, uid)
                return
            except ValueError:
                pass

    # --- Абонементы: экран управления (админка) ---
    if data.startswith("admin_sub_manage_"):
        sid = _extract_int(data, "admin_sub_manage_")
        if sid is not None:
            await show_admin_sub_manage(update, context, sid)
            return

    # Выдача нового абонемента: выбор тарифа
    if data.startswith("admin_sub_create_"):
        uid = _extract_int(data, "admin_sub_create_")
        if uid is not None:
            await admin_sub_create_handler(update, context, uid)
            return

    # Выдача нового абонемента: подтверждение созданием
    if data.startswith("admin_sub_type_"):
        parsed = _extract_two_ints(data, "admin_sub_type_")
        if parsed:
            uid, tid = parsed
            await admin_sub_type_handler(update, context, uid, tid)
            return

    # Быстрое изменение часов
    if data.startswith("admin_sub_add_hours_"):
        sid = _extract_int(data, "admin_sub_add_hours_")
        if sid is not None:
            await handle_sub_add_hours(update, context, sid)
            return

    if data.startswith("admin_sub_sub_hours_"):
        sid = _extract_int(data, "admin_sub_sub_hours_")
        if sid is not None:
            await handle_sub_sub_hours(update, context, sid)
            return

    # Смена даты окончания (обычно перехватывает ConversationHandler)
    if data.startswith("admin_sub_expires_"):
        sid = _extract_int(data, "admin_sub_expires_")
        if sid is not None:
            await admin_sub_expires_handler(update, context)
            return

    # Отметка посещения: выбор часов (1 / 1.5)
    if data.startswith("admin_sub_visit_hours_"):
        parts = data[len("admin_sub_visit_hours_"):].split("_", 1)
        if len(parts) == 2:
            try:
                sid = int(parts[0])
                hours = float(parts[1])
                await admin_sub_visit_hours_handler(update, context, sid, hours)
                return
            except ValueError:
                pass

    if data.startswith("admin_sub_visit_"):
        sid = _extract_int(data, "admin_sub_visit_")
        if sid is not None:
            await admin_sub_visit_handler(update, context, sid)
            return

    # Отмена абонемента (сначала — подтверждение, затем — исполнение)
    if data.startswith("admin_sub_cancel_yes_"):
        sid = _extract_int(data, "admin_sub_cancel_yes_")
        if sid is not None:
            await admin_sub_cancel_yes_handler(update, context, sid)
            return

    if data.startswith("admin_sub_cancel_"):
        sid = _extract_int(data, "admin_sub_cancel_")
        if sid is not None:
            await admin_sub_cancel_handler(update, context, sid)
            return

    # --- Панель преподавателя ---
    if data == "teacher_panel":
        await show_teacher_panel(update, context)
        return

    if data == "teacher_today":
        await show_teacher_today(update, context)
        return

    if data == "teacher_week":
        await show_teacher_week(update, context)
        return

    if data.startswith("teacher_slot_"):
        slot_id = _extract_int(data, "teacher_slot_")
        if slot_id is not None:
            await show_teacher_slot(update, context, slot_id)
            return

    # --- Остальные пункты ---
    if data == "prices":
        text = (
            "💰 СТОИМОСТЬ АБОНЕМЕНТОВ:\n\n"
            "▫️ 25 / 30 руб — Разовое (1 / 1.5 часа)\n"
            "▫️ 90 / 110 руб — 4 занятия (1 / 1.5 часа)\n"
            "▫️ 145 / 185 руб — 8 занятий (1 / 1.5 часа)\n"
            "▫️ 185 / 235 руб — 12 занятий (1 / 1.5 часа)"
        )
    elif data == "info":
        text = (
            "📍 О СТУДИИ\n\n"
            "📞 Телефоны:\n"
            "Тел.: +375 (29) 351 23 61\n"
            "Тел.: +375 (29) 751 23 61\n\n"
            "🏠 Адреса:\n"
            "Шорная 20-12Н — https://soul.by/zal1/\n"
            "Шорная 20-4Н — https://soul.by/zal2/\n\n"
            "🗺️ Карта:\n"
            "https://yandex.by/maps/?ll=27.54178400,53.90070400&z=17"
        )
    elif data == "support":
        text = (
            "💬 Задать вопрос\n\n"
            "Просто напишите ваш вопрос сюда — я отвечу!\n\n"
            "Или используйте команду: /ask <вопрос>"
        )
    elif data == "book":
        # Этот случай обрабатывается ConversationHandler, но на случай
        # если он не сработал — покажем сообщение
        text = "✍️ Запись на занятие..."
        await query.edit_message_text(text)
        return
    else:
        text = "Неизвестная команда."

    # Под каждым информационным сообщением — кнопка возврата в главное меню
    back_keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")],
        ]
    )
    await query.edit_message_text(text, reply_markup=back_keyboard)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler для регистрации
    book_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(book_start, pattern="^book$")],
        states={
            CONSENT: [CallbackQueryHandler(book_consent, pattern="^consent_agree$")],
            FULL_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, book_full_name)
            ],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_phone)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # ConversationHandler для редактирования профиля
    profile_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(profile_edit_start, pattern="^profile_edit$")],
        states={
            PROFILE_FULL_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, profile_edit_full_name),
                CallbackQueryHandler(profile_cancel, pattern="^profile_cancel$"),
            ],
            PROFILE_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, profile_edit_phone),
                CallbackQueryHandler(profile_cancel, pattern="^profile_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", profile_cancel)],
    )

    # ConversationHandler смены даты окончания абонемента (админка)
    admin_sub_expires_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_sub_expires_handler, pattern=r"^admin_sub_expires_\d+$")
        ],
        states={
            ADMIN_SUB_EXPIRES_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_sub_expires_date),
                CallbackQueryHandler(
                    admin_sub_expires_cancel, pattern=r"^admin_sub_expires_cancel$"
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_sub_expires_cancel)],
    )

    # -----------------------------------------------------------------------
    # Error handler — логирует необработанные исключения
    # -----------------------------------------------------------------------
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
        """Логирует ошибки и сообщает пользователю о сбое."""
        logger.error("Необработанная ошибка:", exc_info=context.error)
        if update and hasattr(update, "effective_message") and update.effective_message:
            await update.effective_message.reply_text(
                "\u26a0\ufe0f Что-то пошло не так. Попробуйте ещё раз или напишите /start."
            )

    application.add_error_handler(error_handler)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ask", ask_handler))
    application.add_handler(book_conv)
    application.add_handler(profile_conv)
    application.add_handler(admin_sub_expires_conv)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    application.add_handler(CallbackQueryHandler(button_handler))

    # Запуск в режиме polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
