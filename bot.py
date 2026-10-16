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
    Freeze,
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


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Отправляет уведомление администратору (ADMIN_CHAT_ID из .env)."""
    if not ADMIN_CHAT_ID:
        logger.info("ADMIN_CHAT_ID не задан — уведомление не отправлено")
        return False
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
        return True
    except Exception as e:
        logger.error("Не удалось отправить уведомление админу: %s", e)
        return False


# Состояния для диалога регистрации
CONSENT, FULL_NAME, PHONE = range(3)

# Состояния для диалога редактирования профиля
PROFILE_FULL_NAME, PROFILE_PHONE = range(2)

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
        # Безлимит
        lines.append("\u23f3 Безлимит")
    else:
        n_one = math.floor(hours_left / 1.0)
        n_half = math.floor(hours_left / 1.5)
        lines.append(f"Осталось: {hours_left} часов")
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
async def show_sub_freeze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Заморозка абонемента — заглушка."""
    query = update.callback_query
    await safe_answer(query)
    text = "\U0001f9ca Заморозка будет доступна в ближайшее время."
    keyboard = [[InlineKeyboardButton("\U0001f519 В меню", callback_data="back_to_main")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


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


async def show_sub_freeze_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена активной заморозки клиентом."""
    query = update.callback_query
    await safe_answer(query)

    telegram_id = update.effective_user.id
    user = User.get_by_telegram_id(telegram_id)
    if not user:
        await query.edit_message_text("Ошибка загрузки. Нажмите /start.")
        return

    sub = Subscription.get_active_for_user(user["id"])
    if not sub:
        text = "Активный абонемент не найден."
    else:
        freeze = Freeze.get_active_for_subscription(sub["id"])
        if not freeze:
            text = "У вас нет активной заморозки."
        else:
            Freeze.cancel(freeze["id"])
            logger.info(
                "Пользователь %s отменил заморозку %s", user["id"], freeze["id"]
            )
            text = "\u274c Заморозка отменена."

    keyboard = [[InlineKeyboardButton("\U0001f519 В меню", callback_data="back_to_main")]]
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
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    application.add_handler(CallbackQueryHandler(button_handler))

    # Запуск в режиме polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
