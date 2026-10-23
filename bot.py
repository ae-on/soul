import asyncio
import json
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
    AuditLog,
    Booking,
    EventCache,
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


async def _safe_edit(query, text: str, reply_markup=None):
    """Редактирует сообщение, игнорируя «Message is not modified»."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise


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


def _parse_sub_hours(data: str, prefix: str) -> Optional[tuple[int, float]]:
    """Разбирает «prefix{sub_id}_{hours}» -> (sub_id, hours); None при неудаче."""
    parts = data[len(prefix):].split("_", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), float(parts[1])
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

# Состояния диалога создания заморозки админом (даты начала/окончания)
ADMIN_FREEZE_START, ADMIN_FREEZE_END = 11, 12

# Состояния диалога редактирования профиля (супер-админ)
ADMIN_PROFILE_FIELD, ADMIN_PROFILE_VALUE = 13, 14

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


async def sync_events_cache() -> int:
    """
    Синхронизирует events_cache с EM API: получает события, определяет
    teacher_id (из owner или сопоставления со слотами), upsert в кэш.
    Возвращает количество обработанных событий.
    """
    events = await get_upcoming_events()
    if not events:
        return 0

    # Загружаем все слоты для сопоставления по категории+времени
    all_slots = Group.get_active()  # type: ignore[attr-defined]

    count = 0
    for ev in events:
        em_id = ev.get("id")
        if not em_id:
            continue

        name = ev.get("name") or ""
        start_date = ev.get("event_start_date")
        start_time = ev.get("event_start_time")
        end_time = ev.get("event_end_time")
        category_slug = ev.get("category_slug") or ""
        location_id = ev.get("location_id")
        location_name = ev.get("location_name") or ""

        # Пытаемся определить teacher_id
        teacher_id = None

        # 1. Из owner события (если EM API передаёт владельца)
        owner = ev.get("owner") or {}
        if isinstance(owner, dict):
            owner_name = owner.get("name") or ""
            if owner_name:
                # Ищем пользователя по full_name (простейшее сопоставление)
                user = User.get_by_field(full_name=owner_name)
                if user:
                    roles = UserRole.get_active_roles(user[0]["id"])
                    if "teacher" in roles or "admin" in roles:
                        teacher_id = user[0]["id"]
        elif isinstance(owner, str) and owner:
            user = User.get_by_field(full_name=owner)
            if user:
                roles = UserRole.get_active_roles(user[0]["id"])
                if "teacher" in roles or "admin" in roles:
                    teacher_id = user[0]["id"]

        # 2. Если не нашли по owner — сопоставляем со слотами
        if teacher_id is None and start_time and category_slug:
            # Приводим время из EM (может быть "18:00:00" или "18:00") к формату для сравнения
            ev_time_str = str(start_time)[:5]
            for slot in all_slots:
                slot_time = _format_time(slot.get("time_start"))
                if slot_time == ev_time_str and slot.get("category_slug") == category_slug:
                    teacher_id = slot.get("teacher_id")
                    if teacher_id:
                        break

        # Upsert в кэш
        try:
            EventCache.upsert(
                em_event_id=em_id,
                name=name,
                start_date=start_date,
                start_time=start_time,
                end_time=end_time,
                location_id=location_id,
                location_name=location_name,
                category_slug=category_slug,
                teacher_id=teacher_id,
            )
            count += 1
        except Exception as e:
            logger.error("Ошибка при сохранении события %s: %s", em_id, e)

    logger.info("Синхронизация кэша событий: %d обработано", count)
    return count


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
                "\U0001f4b0 Мой абонемент", callback_data="my_subscription"
            )
        ]
    )
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

    # Активная заморозка — определяем заранее (для пометки у «Действует до»)
    active_freeze = Freeze.get_active_for_subscription(sub["id"])

    expires_at = sub.get("expires_at")
    if expires_at:
        days_left = (expires_at.date() - date.today()).days
        freeze_note = " (с учётом заморозки)" if active_freeze else ""
        lines.append(
            f"Действует до: {_format_date(expires_at)} ({days_left} дней){freeze_note}"
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
    ]

    # Кнопки заморозки — только если ещё не использована
    if (sub.get("freeze_used") or 0) == 0:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f9ca Заморозить сейчас", callback_data="freeze_quick"
                ),
                InlineKeyboardButton(
                    "\U0001f4c5 Заморозить на даты", callback_data="freeze_custom"
                ),
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "\U0001f3e2 Купить на занятии", callback_data="sub_buy_offline"
            )
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                "\U0001f6d2 Продлить онлайн (ЕРИП)", callback_data="sub_buy_online"
            )
        ]
    )
    keyboard.append(
        [InlineKeyboardButton("\U0001f4d6 Правила пользования", url=RULES_URL)]
    )

    # Активная заморозка — дополнительный блок и кнопка отмены
    if active_freeze:
        freeze_days_left = (active_freeze["end_date"] - date.today()).days
        text += (
            "\n\n\U0001f9ca Активная заморозка: "
            f"с {_format_date(active_freeze['start_date'])} "
            f"по {_format_date(active_freeze['end_date'])}\n"
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
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_subscription_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Объединённая история: покупки, заморозки, посещения (последние 20)."""
    query = update.callback_query
    await safe_answer(query)

    telegram_id = update.effective_user.id
    user = User.get_by_telegram_id(telegram_id)
    if not user:
        await query.edit_message_text("Ошибка загрузки. Нажмите /start.")
        return

    items = User.get_history(user["id"], limit=20)

    if not items:
        text = "\U0001f4c5 История\n\nПока нет записей."
    else:
        lines = ["\U0001f4c5 История (последние 20)", ""]
        for item in items:
            date_str = _format_date(item.get("date"))
            item_type = item.get("type")

            if item_type == "purchase":
                type_name = item.get("type_name") or "Абонемент"
                hours = item.get("hours")
                hours_str = f"{hours}ч" if hours is not None else "безлимит"
                lines.append(
                    f"\U0001f4e6 {date_str} · Куплен абонемент \"{type_name}\" ({hours_str})"
                )
            elif item_type == "freeze":
                reason = item.get("reason") or "—"
                days = item.get("days")
                days_str = f"{days} дней" if days is not None else "—"
                lines.append(
                    f"\U0001f9ca {date_str} · Заморозка: {reason} ({days_str})"
                )
            elif item_type == "audit":
                # Действия администратора (audit_log): например, изменение срока
                line = f"\U0001f6e0\ufe0f {date_str} · Действие администратора"
                try:
                    payload = json.loads(item.get("new_value") or "{}")
                except (ValueError, TypeError):
                    payload = {}
                delta = payload.get("delta")
                if isinstance(delta, (int, float)):
                    line = (
                        f"\U0001f6e0\ufe0f {date_str} · Админ изменил срок "
                        f"({delta:+g} {_plural_days(int(delta))})"
                    )
                lines.append(line)
            else:
                # — visit —
                visit_status = (item.get("status") or "").lower()
                em_event_id = item.get("em_event_id")
                # em_event_id == 0 — ручная отметка админом (без события из EM)
                manual = not em_event_id
                hours = item.get("hours")
                hours_str = f"{hours}ч" if hours is not None else "—"

                if visit_status in ("present", "attended", "registered"):
                    icon = "\u2705"
                    if manual:
                        line = f"{date_str} · \u270d\ufe0f Ручное списание · {hours_str}"
                    else:
                        event_name = item.get("event_name") or f"Событие #{em_event_id}"
                        line = f"{date_str} · {event_name} · {hours_str}"
                elif manual:
                    # Ручная отметка с пропуском
                    icon = "\u26a0\ufe0f"
                    line = f"{date_str} · \u270d\ufe0f Ручной пропуск"
                elif visit_status == "cancelled":
                    icon = "\u274c"
                    event_name = item.get("event_name") or f"Событие #{em_event_id}"
                    line = f"{date_str} · {event_name} · отменено"
                else:
                    # absent / no_show / late_cancel / skipped
                    icon = "\u26a0\ufe0f"
                    event_name = item.get("event_name") or f"Событие #{em_event_id}"
                    line = f"{date_str} · {event_name} · пропуск"
                lines.append(f"{icon} {line}")
        text = "\n".join(lines)

    keyboard = [
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="my_subscription")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ----------------------------------------------------------------------
# Заглушки: заморозка, продление, покупка (полная логика — позже)
# ----------------------------------------------------------------------
def _plural_days(n: int) -> str:
    """Склонение слова «день»: 1 день, 3 дня, 5 дней."""
    n = abs(n)
    if 11 <= n % 100 <= 14:
        return "дней"
    if n % 10 == 1:
        return "день"
    if n % 10 in (2, 3, 4):
        return "дня"
    return "дней"


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
    # Защита от двойного нажатия: обрабатываем только pending-заявки
    if freeze["status"] != "pending":
        await safe_answer(query, "Эта заявка уже обработана", show_alert=True)
        return
    # Защита: у абонемента не должно быть другой активной заморозки
    if Freeze.get_active_for_subscription(freeze["subscription_id"]):
        await safe_answer(query, "У абонемента уже есть активная заморозка", show_alert=True)
        return

    Freeze.approve(freeze_id, admin["id"])
    Subscription.update_freeze_used(freeze["subscription_id"], 1)
    logger.info("Админ %s одобрил заморозку %s", admin["id"], freeze_id)

    # Уведомляем клиента (срок продлится при завершении заморозки)
    sub = Subscription.get_by_id(freeze["subscription_id"])
    client = User.get_by_id(sub["user_id"]) if sub else None
    if client and client.get("telegram_id"):
        try:
            await context.bot.send_message(
                chat_id=client["telegram_id"],
                text=(
                    "\u2705 Ваша заморозка подтверждена "
                    f"с {_format_date(freeze['start_date'])} "
                    f"по {_format_date(freeze['end_date'])}.\n"
                    "Срок абонемента продлится, когда заморозка завершится."
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
    Использует единую complete_and_revert: корректирует срок, сбрасывает freeze_used.
    Вызывается на /start и при отметке посещения.
    """
    sub = Subscription.get_active_for_user(user_id)
    if not sub:
        return False

    freeze = Freeze.get_approved_started(sub["id"])
    if not freeze:
        return False

    Freeze.complete_and_revert(freeze["id"], date.today())
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
        [InlineKeyboardButton("\U0001f465 Клиенты", callback_data="admin_clients")],
        [InlineKeyboardButton("\U0001f4c5 Слоты и группы", callback_data="admin_slots")],
        [InlineKeyboardButton("\U0001f9d1\u200d\U0001f3eb Учителя", callback_data="admin_teachers")],
        [InlineKeyboardButton("\U0001f527 Админы", callback_data="admin_admins")],
        [InlineKeyboardButton("\U0001f519 Главное меню", callback_data="back_to_main")],
    ]
    text = "\U0001f527 Панель администратора"
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


# Число клиентов на страницу в списке админки
CLIENTS_PER_PAGE = 10

# Эмодзи для статусов пользователей в списках админки
USER_STATUS_EMOJI = {
    "new": "\U0001f195",
    "active": "\u2705",
    "expired": "\u23f3",
    "archived": "\U0001f4e6",
    "inactive": "\u26aa",
    "deleted_data": "\U0001f5d1\ufe0f",
}


def _format_user_line(idx: int, u: dict) -> str:
    """Строка списка: «1. Иванов Александр (ID: 10000000) — ✅ active»."""
    name = u.get("full_name") or u.get("username") or "—"
    emoji = USER_STATUS_EMOJI.get(u.get("status"), "\u2022")
    return f"{idx}. {name} (ID: {u.get('telegram_id')}) — {emoji} {u.get('status')}"


def _short_name(full_name: str) -> str:
    """«Иванов Александр» → «Иванов А.»."""
    parts = full_name.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return full_name or "—"


def _client_button_label(u: dict) -> str:
    """Подпись кнопки клиента: «Иванов А. · 8ч» / «Петров И. · нет абона» / «Сидоров П. · новый»."""
    name = _short_name(u.get("full_name") or u.get("username") or f"ID {u.get('telegram_id')}")
    hours_left = u.get("hours_left")
    if hours_left is not None:
        # Decimal из MySQL приводим к float и убираем лишние нули (8.0 → 8)
        suffix = f"{float(hours_left):g}ч"
    elif u.get("status") == "new":
        suffix = "новый"
    else:
        suffix = "нет абона"
    return f"{name} · {suffix}"


async def show_admin_clients(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 1,
    status_filter: str = "all",
):
    """Список клиентов (роль student) с пагинацией и фильтром по статусу."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    total = User.count_by_role("student", status_filter=status_filter)
    total_pages = max(1, math.ceil(total / CLIENTS_PER_PAGE))

    # Защита от выхода за границы пагинации
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages

    offset = (page - 1) * CLIENTS_PER_PAGE
    # Клиенты с остатком часов активного абонемента (для кнопок)
    users = User.get_clients_for_admin(
        limit=CLIENTS_PER_PAGE, offset=offset, status_filter=status_filter
    )

    # Фильтры по статусу (всегда сбрасывают на первую страницу)
    filter_row = [
        InlineKeyboardButton("\U0001f195 Новые", callback_data="admin_clients_page_1_new"),
        InlineKeyboardButton("\u2705 Активные", callback_data="admin_clients_page_1_active"),
        InlineKeyboardButton("\U0001f4e6 Все", callback_data="admin_clients_page_1_all"),
    ]

    if total == 0:
        # Пустой список: фильтры остаются, пагинация скрыта
        text = "\U0001f465 Клиенты (Всего: 0)\n\nНичего не найдено."
        keyboard = [
            filter_row,
            [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")],
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))
        return

    text = f"\U0001f465 Клиенты (Всего: {total})\nВыберите клиента для управления:"

    keyboard = [filter_row]
    # Клики по кнопкам клиентов — карточка пользователя
    for u in users:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"\U0001f464 {_client_button_label(u)}",
                    callback_data=f"admin_user_{u['id']}",
                )
            ]
        )

    # Навигация: стрелки только если есть куда листать
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(
                InlineKeyboardButton(
                    "\u2b05\ufe0f",
                    callback_data=f"admin_clients_page_{page - 1}_{status_filter}",
                )
            )
        nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav.append(
                InlineKeyboardButton(
                    "\u27a1\ufe0f",
                    callback_data=f"admin_clients_page_{page + 1}_{status_filter}",
                )
            )
        keyboard.append(nav)

        # Номера страниц [1] [2] [3] ... (активная страница — в скобках)
        page_buttons = []
        for p in range(1, total_pages + 1):
            label = f"[{p}]" if p == page else str(p)
            page_buttons.append(
                InlineKeyboardButton(label, callback_data=f"admin_clients_page_{p}_{status_filter}")
            )
        for i in range(0, len(page_buttons), 5):
            keyboard.append(page_buttons[i : i + 5])

    keyboard.append(
        [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")]
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_admin_teachers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список учителей (роль teacher), без пагинации."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    users = User.get_by_role("teacher", limit=100, offset=0, status_filter="all")
    total = User.count_by_role("teacher")

    if not users:
        text = f"\U0001f9d1\u200d\U0001f3eb Учителя (Всего: {total})\n\nНет пользователей."
        keyboard = [
            [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")]
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))
        return

    lines = [f"\U0001f9d1\u200d\U0001f3eb Учителя (Всего: {total})", ""]
    for i, u in enumerate(users, 1):
        lines.append(_format_user_line(i, u))
    text = "\n".join(lines)

    keyboard = []
    for u in users:
        name = u.get("full_name") or u.get("username") or f"ID {u.get('telegram_id')}"
        keyboard.append(
            [InlineKeyboardButton(f"\U0001f464 {name}", callback_data=f"admin_user_{u['id']}")]
        )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")]
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_admin_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список администраторов (роли admin и super_admin), без пагинации."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    # Объединяем admin и super_admin, дедуплицируем по id
    admins = User.get_by_role("admin", limit=100, offset=0, status_filter="all")
    super_admins = User.get_by_role("super_admin", limit=100, offset=0, status_filter="all")
    merged = {u["id"]: u for u in admins + super_admins}
    users = sorted(merged.values(), key=lambda u: (u.get("full_name") or "").lower())

    if not users:
        text = "\U0001f527 Админы\n\nНет пользователей."
        keyboard = [
            [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")]
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))
        return

    lines = [f"\U0001f527 Админы (Всего: {len(users)})", ""]
    for i, u in enumerate(users, 1):
        lines.append(_format_user_line(i, u))
    text = "\n".join(lines)

    keyboard = []
    for u in users:
        name = u.get("full_name") or u.get("username") or f"ID {u.get('telegram_id')}"
        keyboard.append(
            [InlineKeyboardButton(f"\U0001f464 {name}", callback_data=f"admin_user_{u['id']}")]
        )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")]
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


# ----------------------------------------------------------------------
# Админка: управление слотами (группами)
# ----------------------------------------------------------------------
SLOTS_PER_PAGE = 10


async def show_admin_slots(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1
):
    """Список всех слотов с пагинацией."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    total = Group.count_all()
    total_pages = max(1, math.ceil(total / SLOTS_PER_PAGE))
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages

    offset = (page - 1) * SLOTS_PER_PAGE
    slots = Group.get_all_with_teachers(limit=SLOTS_PER_PAGE, offset=offset)

    if not slots:
        text = "\U0001f4c5 Слоты и группы\n\nНет слотов."
        keyboard = [
            [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")]
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))
        return

    lines = [f"\U0001f4c5 Слоты и группы (Всего: {total})", ""]
    keyboard = []
    for slot in slots:
        name = slot.get("name") or slot.get("category_slug") or "Слот"
        time_str = _format_time(slot.get("time_start"))
        teacher_name = slot.get("teacher_name") or "Не назначен"
        lines.append(f"• {name} {time_str} — {teacher_name}")
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"\U0001f4c5 {name} {time_str}",
                    callback_data=f"admin_slot_manage_{slot['id']}",
                )
            ]
        )

    # Пагинация
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("\u2b05\ufe0f", callback_data=f"admin_slots_page_{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("\u27a1\ufe0f", callback_data=f"admin_slots_page_{page + 1}"))
        keyboard.append(nav)

        page_buttons = []
        for p in range(1, total_pages + 1):
            label = f"[{p}]" if p == page else str(p)
            page_buttons.append(InlineKeyboardButton(label, callback_data=f"admin_slots_page_{p}"))
        for i in range(0, len(page_buttons), 5):
            keyboard.append(page_buttons[i : i + 5])

    keyboard.append(
        [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")]
    )
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard))


async def show_admin_slot_manage(
    update: Update, context: ContextTypes.DEFAULT_TYPE, slot_id: int
):
    """Управление конкретным слотом."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    slot = Group.get_by_id(slot_id)
    if not slot:
        await safe_answer(query, "Слот не найден", show_alert=True)
        return

    # Получаем имя преподавателя
    teacher_name = "Не назначен"
    if slot.get("teacher_id"):
        teacher = User.get_by_id(slot["teacher_id"])
        if teacher:
            teacher_name = teacher.get("full_name") or teacher.get("username") or str(slot["teacher_id"])

    weekdays_map = {"1": "Пн", "2": "Вт", "3": "Ср", "4": "Чт", "5": "Пт", "6": "Сб", "7": "Вс"}
    weekdays_str = ", ".join(
        weekdays_map.get(d.strip(), d.strip())
        for d in (slot.get("weekdays") or "").split(",")
        if d.strip()
    )

    text = (
        f"\u2699\ufe0f Слот: {slot.get('name') or slot.get('category_slug') or '—'}\n"
        f"\U0001f550 Время: {_format_time(slot.get('time_start'))} — {_format_time(slot.get('time_end'))} ({weekdays_str})\n"
        f"\U0001f9d1\u200d\U0001f3eb Преподаватель: {teacher_name}"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f9d1\u200d\U0001f3eb Назначить преподавателя",
                callback_data=f"admin_slot_assign_{slot_id}",
            )
        ],
        [InlineKeyboardButton("\U0001f519 К списку слотов", callback_data="admin_slots")],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_admin_slot_assign(
    update: Update, context: ContextTypes.DEFAULT_TYPE, slot_id: int
):
    """Список преподавателей для назначения на слот."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    slot = Group.get_by_id(slot_id)
    if not slot:
        await safe_answer(query, "Слот не найден", show_alert=True)
        return

    teachers = User.get_by_role("teacher", limit=100, offset=0, status_filter="all")

    if not teachers:
        text = "Нет пользователей с ролью teacher."
        keyboard = [
            [InlineKeyboardButton("\U0001f519 Назад", callback_data=f"admin_slot_manage_{slot_id}")]
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))
        return

    lines = [
        "\U0001f9d1\u200d\U0001f3eb Назначение преподавателя",
        f"Слот: {slot.get('name') or slot.get('category_slug') or '—'}",
        "",
    ]
    keyboard = []
    for t in teachers:
        name = t.get("full_name") or t.get("username") or f"ID {t.get('telegram_id')}"
        lines.append(f"• {name}")
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"\U0001f9d1\u200d\U0001f3eb {name}",
                    callback_data=f"admin_slot_set_teacher_{slot_id}_{t['id']}",
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data=f"admin_slot_manage_{slot_id}")]
    )
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard))


async def handle_admin_slot_set_teacher(
    update: Update, context: ContextTypes.DEFAULT_TYPE, slot_id: int, teacher_id: int
):
    """Назначить преподавателя на слот."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    try:
        Group.update(slot_id, teacher_id=teacher_id)
        logger.info(
            "Админ %s назначил преподавателя %s на слот %s",
            admin["id"], teacher_id, slot_id,
        )
        await safe_answer(query, "\u2705 Преподаватель назначен", show_alert=True)
    except Exception as e:
        logger.error("Ошибка при назначении преподавателя: %s", e)
        await safe_answer(query, "Ошибка при назначении", show_alert=True)

    await show_admin_slot_manage(update, context, slot_id)


async def show_admin_slot_create_stub(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Заглушка создания слота."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    text = (
        "\u2795 Создание слотов\n\n"
        "Создание слотов через админку появится в ближайшее время.\n"
        "Пока добавляйте их через базу данных."
    )
    keyboard = [
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_slots")]
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


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
    ]

    # Редактирование профиля доступно только супер-админам
    viewer_roles = UserRole.get_active_roles(admin["id"])
    if "super_admin" in viewer_roles:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\u2699\ufe0f Редактировать профиль",
                    callback_data=f"admin_profile_edit_{user_id}",
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_clients_page_1_all")]
    )
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


# ----------------------------------------------------------------------
# Редактирование профиля пользователя (только super_admin)
# ----------------------------------------------------------------------
ADMIN_PROFILE_FIELDS = {
    "name": "ФИО",
    "phone": "Телефон",
    "email": "Email",
    "notes": "Заметки",
}


async def admin_profile_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню редактирования профиля (доступно только super_admin)."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END
    query = update.callback_query

    if "super_admin" not in UserRole.get_active_roles(admin["id"]):
        await safe_answer(query, "Нет доступа", show_alert=True)
        return ConversationHandler.END

    # ConversationHandler вызывает обработчик только с (update, context) —
    # ID пользователя достаём из callback_data
    try:
        user_id = int(query.data.replace("admin_profile_edit_", ""))
    except (ValueError, AttributeError):
        await query.edit_message_text("Ошибка: неверный ID пользователя.")
        return ConversationHandler.END

    target = User.get_by_id(user_id)
    if not target:
        await safe_answer(query, "Пользователь не найден", show_alert=True)
        return ConversationHandler.END

    context.user_data["admin_profile_user_id"] = user_id

    text = (
        f"\u2699\ufe0f Редактирование профиля: "
        f"{target.get('full_name') or target.get('username') or user_id}\n\n"
        "Что изменить?"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "\u270f\ufe0f ФИО", callback_data="admin_profile_field_name"
            ),
            InlineKeyboardButton(
                "\U0001f4f1 Телефон", callback_data="admin_profile_field_phone"
            ),
        ],
        [
            InlineKeyboardButton(
                "\U0001f4e7 Email", callback_data="admin_profile_field_email"
            ),
            InlineKeyboardButton(
                "\U0001f5d2\ufe0f Заметки", callback_data="admin_profile_field_notes"
            ),
        ],
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_profile_cancel")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_PROFILE_FIELD


async def admin_profile_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор поля — запрос нового значения."""
    query = update.callback_query
    await safe_answer(query)

    field_key = query.data.replace("admin_profile_field_", "")
    if field_key not in ADMIN_PROFILE_FIELDS:
        return ADMIN_PROFILE_FIELD

    context.user_data["admin_profile_field"] = field_key
    label = ADMIN_PROFILE_FIELDS[field_key]
    text = (
        f"Введите новое значение для «{label}»:\n\n"
        "(или нажмите /cancel чтобы отменить)"
    )
    keyboard = [[InlineKeyboardButton("\U0001f519 Отмена", callback_data="admin_profile_cancel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_PROFILE_VALUE


async def admin_profile_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Валидация и сохранение нового значения поля."""
    value = update.message.text.strip()
    field_key = context.user_data.get("admin_profile_field")
    user_id = context.user_data.get("admin_profile_user_id")
    if not field_key or not user_id:
        await update.message.reply_text("Диалог устарел. Откройте редактирование заново.")
        return ConversationHandler.END

    # Валидация
    if field_key == "name" and len(value) < 3:
        await update.message.reply_text(
            "\u26a0\ufe0f ФИО должно содержать минимум 3 символа. Введите ещё раз:"
        )
        return ADMIN_PROFILE_VALUE
    if field_key == "phone":
        digits = re.sub(r"\D", "", value)
        if len(digits) < 7:
            await update.message.reply_text(
                "\u26a0\ufe0f В телефоне должно быть минимум 7 цифр. Введите ещё раз:"
            )
            return ADMIN_PROFILE_VALUE
    if field_key == "email" and not re.match(r"^[^@\s]+@[^@\s]+$", value):
        await update.message.reply_text(
            "\u26a0\ufe0f Неверный email. Введите ещё раз:"
        )
        return ADMIN_PROFILE_VALUE

    # Сохраняем
    if field_key == "name":
        User.update(user_id, full_name=value)
    elif field_key == "phone":
        User.update(user_id, phone=value)
    elif field_key == "email":
        User.update(user_id, email=value)
    elif field_key == "notes":
        User.update(user_id, notes=value)

    label = ADMIN_PROFILE_FIELDS[field_key]
    logger.info(
        "Супер-админ (telegram %s) изменил поле %s пользователя %s",
        update.effective_user.id,
        label,
        user_id,
    )

    await update.message.reply_text(f"\u2705 {label} обновлено.")
    context.user_data.pop("admin_profile_field", None)
    context.user_data.pop("admin_profile_user_id", None)
    # Возвращаемся к карточке пользователя
    await show_admin_user_card(update, context, user_id)
    return ConversationHandler.END


async def admin_profile_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена редактирования профиля (кнопка или /cancel)."""
    user_id = context.user_data.get("admin_profile_user_id")
    context.user_data.pop("admin_profile_field", None)
    context.user_data.pop("admin_profile_user_id", None)

    if update.callback_query:
        await safe_answer(update.callback_query)
        await update.callback_query.edit_message_text(
            "\u2699\ufe0f Редактирование отменено."
        )
    else:
        await update.message.reply_text("\u2699\ufe0f Редактирование отменено.")

    if user_id:
        await show_admin_user_card(update, context, user_id)
    return ConversationHandler.END


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

    # --- Остаток часов ---
    hours_left = sub.get("hours_left")
    if hours_left is None:
        if sub_type and "безлимит" in (sub_type.get("name") or "").lower():
            hours_line = "Осталось: Безлимит"
        else:
            hours_line = "Осталось: 0ч (ошибка данных)"
    else:
        hours_left_f = float(hours_left)
        # Определяем продолжительность одного занятия (1.0 или 1.5)
        type_name_lower = (type_name or "").lower()
        per_class = 1.5 if "1.5" in type_name_lower else 1.0
        classes = int(hours_left_f / per_class)
        hours_line = f"Осталось: {hours_left_f}ч ({classes} зан.)"

    # --- Период ---
    started_at = sub.get("started_at")
    expires_at = sub.get("expires_at")
    period_line = (
        f"Период: {_format_date(started_at)} — {_format_date(expires_at)}"
    )

    # --- Активная заморозка ---
    active_freeze = Freeze.get_active_for_subscription(sub_id)
    if active_freeze:
        freeze_days = active_freeze.get("days") or 0
        freeze_line = (
            f"\U0001f9ca Активная заморозка: "
            f"с {_format_date(active_freeze['start_date'])} "
            f"по {_format_date(active_freeze['end_date'])} "
            f"(+{freeze_days} дней к сроку)"
        )
    else:
        freeze_line = "\U0001f9ca Заморозка не активна."

    # --- Ручные корректировки ---
    adj_total = Subscription.get_manual_adjustments(sub_id)
    if adj_total > 0:
        adj_line = f"\U0001f4dd Ручные корректировки: +{adj_total} дней"
    elif adj_total < 0:
        adj_line = f"\U0001f4dd Ручные корректировки: {adj_total} дней"
    else:
        adj_line = ""  # ничего не выводим если корректировок нет

    # --- Собираем текст ---
    lines = [f"\u2699\ufe0f Управление: {type_name}", hours_line, period_line, freeze_line]
    if adj_line:
        lines.append(adj_line)
    text = "\n".join(lines)
    if notice:
        text = f"{notice}\n\n{text}"

    keyboard = [
        [
            InlineKeyboardButton(
                "\u2795 1.0ч", callback_data=f"admin_sub_add_hours_{sub_id}_1.0"
            ),
            InlineKeyboardButton(
                "\u2796 1.0ч", callback_data=f"admin_sub_sub_hours_{sub_id}_1.0"
            ),
        ],
        [
            InlineKeyboardButton(
                "\u2795 1.5ч", callback_data=f"admin_sub_add_hours_{sub_id}_1.5"
            ),
            InlineKeyboardButton(
                "\u2796 1.5ч", callback_data=f"admin_sub_sub_hours_{sub_id}_1.5"
            ),
        ],
        # Изменение срока: +/- дни
        [
            InlineKeyboardButton(
                "\u2795 1 день", callback_data=f"admin_sub_add_days_{sub_id}_1"
            ),
            InlineKeyboardButton(
                "\u2796 1 день", callback_data=f"admin_sub_add_days_{sub_id}_-1"
            ),
        ],
        [
            InlineKeyboardButton(
                "\u2795 7 дней", callback_data=f"admin_sub_add_days_{sub_id}_7"
            ),
            InlineKeyboardButton(
                "\u2796 7 дней", callback_data=f"admin_sub_add_days_{sub_id}_-7"
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
    ]

    # Управление заморозкой: две кнопки (быстрая / на даты)
    if active_freeze:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f525 Снять заморозку",
                    callback_data=f"admin_sub_unfreeze_{sub_id}",
                )
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f9ca Заморозить сейчас",
                    callback_data=f"freeze_quick_{sub_id}",
                ),
                InlineKeyboardButton(
                    "\U0001f4c5 Заморозить на даты",
                    callback_data=f"freeze_custom_{sub_id}",
                ),
            ]
        )
    # Сброс заморозок доступен всегда
    keyboard.append(
        [
            InlineKeyboardButton(
                "\U0001f504 Сбросить заморозки",
                callback_data=f"admin_sub_reset_freezes_{sub_id}",
            )
        ]
    )

    # Сброс ручных корректировок — только если они есть
    if adj_total != 0:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f504 Сбросить корректировки",
                    callback_data=f"admin_sub_reset_manual_{sub_id}",
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "\u274c Отменить абонемент",
                callback_data=f"admin_sub_cancel_{sub_id}",
            )
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                "\U0001f519 Назад",
                callback_data=f"admin_user_subs_{sub['user_id']}",
            )
        ]
    )
    reply_markup = InlineKeyboardMarkup(keyboard)

    if query:
        await _safe_edit(query, text, reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def handle_sub_add_hours(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int, hours: float
):
    """Добавление часов к абонементу (1.0 / 1.5)."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    if Subscription.update_hours(sub_id, hours):
        logger.info("Админ %s добавил %sч абонементу %s", admin["id"], hours, sub_id)
    await show_admin_sub_manage(update, context, sub_id)


async def handle_sub_sub_hours(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int, hours: float
):
    """Списание часов с абонемента (1.0 / 1.5, не ниже 0)."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    if Subscription.update_hours(sub_id, -hours):
        logger.info("Админ %s списал %sч с абонемента %s", admin["id"], hours, sub_id)
    await show_admin_sub_manage(update, context, sub_id)


async def handle_admin_sub_add_days(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int, delta: int
):
    """Админ сдвигает срок абонемента на delta дней (+/-) с записью в audit_log."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    if not Subscription.add_days(sub_id, delta):
        await safe_answer(query, "Не удалось изменить срок", show_alert=True)
        return

    # Пишем в audit_log
    try:
        AuditLog.create(
            user_id=admin["id"],
            action="add_days",
            table_name="subscriptions",
            record_id=sub_id,
            new_value=json.dumps({"delta": delta}),
        )
    except Exception as e:
        logger.error("Не удалось записать audit_log: %s", e)

    logger.info(
        "Админ %s изменил срок абонемента %s на %+d %s",
        admin["id"],
        sub_id,
        delta,
        _plural_days(delta),
    )
    await show_admin_sub_manage(
        update,
        context,
        sub_id,
        notice=f"\U0001f4c5 Срок изменён на {delta:+d} {_plural_days(delta)}",
    )


async def handle_freeze_quick(update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int = None):
    """
    Быстрая заморозка (14 дней или до конца срока).
    Для админа — sub_id из callback; для клиента — sub_id=None (активный абонемент).
    """
    query = update.callback_query
    await safe_answer(query)

    is_admin = False
    admin = None
    user = None
    if sub_id is not None:
        # Админ: проверяем роль
        admin = await _require_admin(update, context)
        if not admin:
            return
        is_admin = True
        sub = Subscription.get_by_id(sub_id)
        if not sub:
            await safe_answer(query, "Абонемент не найден", show_alert=True)
            return
    else:
        # Клиент: берём активный абонемент
        user, sub = _get_user_and_active_sub(update)
        if not user or not sub:
            await query.edit_message_text("\u26a0\ufe0f Нет активного абонемента.")
            return
        if (sub.get("freeze_used") or 0) > 0:
            await query.edit_message_text("Вы уже использовали заморозку.")
            return
        sub_id = sub["id"]

    if Freeze.get_active_for_subscription(sub_id):
        await safe_answer(query, "Уже есть активная заморозка", show_alert=True)
        return

    expires = sub.get("expires_at")
    if not expires or expires.date() <= date.today():
        await safe_answer(query, "Абонемент истёк", show_alert=True)
        return

    days_allowed = (expires.date() - date.today()).days
    freeze_days = min(FREEZE_MAX_DAYS, days_allowed)
    start = date.today()
    end = start + timedelta(days=freeze_days)

    # days — generated column, НЕ передаём
    freeze_id = Freeze.create(
        subscription_id=sub_id,
        start_date=start,
        end_date=end,
        reason="Быстрая заморозка",
        created_by=admin["id"] if is_admin else user["id"],
        status="approved" if is_admin else "pending",
    )

    if is_admin:
        # Админ: сразу сдвигаем срок
        Subscription.extend_expires(sub_id, freeze_days)
        client = User.get_by_id(sub["user_id"]) if sub.get("user_id") else None
        if client and client.get("telegram_id"):
            try:
                await context.bot.send_message(
                    chat_id=client["telegram_id"],
                    text=f"\U0001f9ca Администратор заморозил ваш абонемент на {freeze_days} дней.",
                )
            except Exception as e:
                logger.error("Не удалось уведомить клиента %s: %s", client["telegram_id"], e)
        logger.info("Админ быстрая заморозка %s: %s дней", sub_id, freeze_days)
        await show_admin_sub_manage(
            update, context, sub_id, notice=f"\U0001f9ca Быстрая заморозка на {freeze_days} дней"
        )
    else:
        # Клиент: заявка на одобрение
        full_name = user.get("full_name") or ""
        admin_msg = (
            "\U0001f9ca Заявка на быструю заморозку\n\n"
            f"Клиент: {full_name or '—'} (ID {user.get('telegram_id')})\n"
            f"Абонемент: #{sub_id}\n"
            f"Период: {_format_date(start)} — {_format_date(end)} ({freeze_days} дней)"
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
        logger.info("Клиент %s запросил быструю заморозку %s", user["id"], freeze_id)
        await query.edit_message_text(
            "\u2705 Заявка на быструю заморозку отправлена. Ждите подтверждения."
        )


async def freeze_custom_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Единый вход для кастомной заморозки (админ и клиент).
    Админ: callback freeze_custom_{sub_id}. Клиент: callback freeze_custom.
    """
    query = update.callback_query
    await safe_answer(query)

    # Определяем, кто вызывает
    user = User.get_by_telegram_id(update.effective_user.id)
    if not user:
        await query.edit_message_text("Ошибка: пользователь не найден.")
        return ConversationHandler.END
    roles = UserRole.get_active_roles(user["id"])
    is_admin = "admin" in roles or "super_admin" in roles

    if is_admin:
        # Админ: sub_id из callback
        try:
            sub_id = int(query.data.replace("freeze_custom_", ""))
        except (ValueError, AttributeError):
            await query.edit_message_text("Ошибка: неверный ID абонемента.")
            return ConversationHandler.END
        context.user_data["freeze_is_admin"] = True
        context.user_data["freeze_admin_id"] = user["id"]
    else:
        # Клиент: активный абонемент
        _, sub = _get_user_and_active_sub(update)
        if not sub:
            await query.edit_message_text("\u26a0\ufe0f Нет активного абонемента.")
            return ConversationHandler.END
        if (sub.get("freeze_used") or 0) > 0:
            await query.edit_message_text("Вы уже использовали заморозку.")
            return ConversationHandler.END
        if Freeze.get_open_for_subscription(sub["id"]):
            await query.edit_message_text("Заморозка уже действует (или заявка на рассмотрении).")
            return ConversationHandler.END
        sub_id = sub["id"]
        context.user_data["freeze_is_admin"] = False
        context.user_data["freeze_client_user_id"] = user["id"]

    context.user_data["freeze_sub_id"] = sub_id
    sub = Subscription.get_by_id(sub_id)
    if not sub:
        await query.edit_message_text("Абонемент не найден.")
        return ConversationHandler.END

    text = (
        "\U0001f4c5 Заморозка на даты\n\n"
        f"Действует до: {_format_date(sub.get('expires_at'))}\n\n"
        "Введите дату НАЧАЛА в формате ДД.ММ.ГГГГ "
        "(или «-» чтобы поставить на сегодня):"
    )
    await query.edit_message_text(text)
    return ADMIN_FREEZE_START


async def freeze_custom_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2 — валидация даты начала, запрос даты окончания."""
    raw = update.message.text.strip()
    if raw == "-":
        start = date.today()
    else:
        start = _parse_freeze_date(raw)
        if start is None:
            await update.message.reply_text(
                "\u26a0\ufe0f Неверный формат. Введите ДД.ММ.ГГГГ (или «-»):"
            )
            return ADMIN_FREEZE_START
        if start < date.today():
            await update.message.reply_text(
                "\u26a0\ufe0f Дата начала должна быть сегодня или в будущем:"
            )
            return ADMIN_FREEZE_START

    sub_id = context.user_data.get("freeze_sub_id")
    sub = Subscription.get_by_id(sub_id) if sub_id else None
    if sub and sub.get("expires_at"):
        exp_date = sub["expires_at"].date() if isinstance(sub["expires_at"], datetime) else sub["expires_at"]
        if start > exp_date:
            await update.message.reply_text(
                f"\u26a0\ufe0f Дата начала ({_format_date(start)}) позже окончания абонемента "
                f"({_format_date(exp_date)}). Введите ещё раз:"
            )
            return ADMIN_FREEZE_START

    context.user_data["freeze_start"] = start
    await update.message.reply_text(
        f"\U0001f4c5 Начало: {_format_date(start)}\n\n"
        "Введите дату ОКОНЧАНИЯ (ДД.ММ.ГГГГ) или «-» для максимального срока:"
    )
    return ADMIN_FREEZE_END


async def freeze_custom_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3 — валидация, создание заморозки, сдвиг срока, уведомление."""
    raw = update.message.text.strip()
    end = _parse_freeze_date(raw) if raw != "-" else None

    sub_id = context.user_data.get("freeze_sub_id")
    start = context.user_data.get("freeze_start")
    is_admin = context.user_data.get("freeze_is_admin", False)
    admin_id = context.user_data.get("freeze_admin_id")
    client_user_id = context.user_data.get("freeze_client_user_id")

    if not sub_id or not start:
        await update.message.reply_text("Диалог устарел. Начните заново.")
        return ConversationHandler.END

    sub = Subscription.get_by_id(sub_id)
    if not sub:
        await update.message.reply_text("Абонемент не найден.")
        return ConversationHandler.END

    # Абонемент не должен быть истёкшим
    if sub.get("expires_at"):
        exp_check = sub["expires_at"].date() if isinstance(sub["expires_at"], datetime) else sub["expires_at"]
        if exp_check <= date.today():
            await update.message.reply_text("\u26a0\ufe0f Абонемент уже истёк, заморозка невозможна.")
            return ConversationHandler.END

    # Максимальная дата окончания: start + 14 дней, но не позже expires_at
    max_end = start + timedelta(days=FREEZE_MAX_DAYS)
    if sub.get("expires_at"):
        exp_date = sub["expires_at"].date() if isinstance(sub["expires_at"], datetime) else sub["expires_at"]
        max_end = min(max_end, exp_date)

    max_days = (max_end - start).days

    if end is None:
        # «-» = максимальный срок
        end = max_end
    else:
        if end <= start:
            await update.message.reply_text(
                "\u26a0\ufe0f Дата окончания должна быть позже даты начала. Введите ещё раз:"
            )
            return ADMIN_FREEZE_END
        if end > max_end:
            await update.message.reply_text(
                f"\u26a0\ufe0f Нельзя заморозить больше, чем осталось дней в абонементе. "
                f"Максимум: {max_days} {_plural_days(max_days)} (до {_format_date(max_end)}). "
                f"Введите ещё раз:"
            )
            return ADMIN_FREEZE_END

    days = (end - start).days

    # days — generated column, НЕ передаём
    freeze_id = Freeze.create(
        subscription_id=sub_id,
        start_date=start,
        end_date=end,
        reason="Кастомная заморозка",
        created_by=admin_id or client_user_id or update.effective_user.id,
        status="approved" if is_admin else "pending",
    )

    context.user_data.pop("freeze_sub_id", None)
    context.user_data.pop("freeze_start", None)
    context.user_data.pop("freeze_is_admin", None)
    context.user_data.pop("freeze_admin_id", None)
    context.user_data.pop("freeze_client_user_id", None)

    if is_admin:
        Subscription.extend_expires(sub_id, days)
        client = User.get_by_id(sub["user_id"]) if sub.get("user_id") else None
        if client and client.get("telegram_id"):
            try:
                await context.bot.send_message(
                    chat_id=client["telegram_id"],
                    text=(
                        "\U0001f9ca Администратор заморозил ваш абонемент "
                        f"с {_format_date(start)} по {_format_date(end)} "
                        f"(+{days} дней к сроку)."
                    ),
                )
            except Exception as e:
                logger.error("Не удалось уведомить клиента %s: %s", client["telegram_id"], e)
        logger.info("Админ кастомная заморозка %s: %s — %s (%s дней)", sub_id, start, end, days)
        await update.message.reply_text(
            f"\u2705 Заморозка создана: {_format_date(start)} — {_format_date(end)} ({days} дней)."
        )
        await show_admin_sub_manage(update, context, sub_id)
    else:
        # Клиент: заявка на одобрение
        user = User.get_by_id(client_user_id) if client_user_id else None
        full_name = user.get("full_name") if user else ""
        admin_msg = (
            "\U0001f9ca Заявка на кастомную заморозку\n\n"
            f"Клиент: {full_name or '—'} (ID {user.get('telegram_id') if user else '—'})\n"
            f"Абонемент: #{sub_id}\n"
            f"Период: {_format_date(start)} — {_format_date(end)} ({days} дней)"
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
        logger.info("Клиент %s запросил кастомную заморозку %s", client_user_id, freeze_id)
        await update.message.reply_text(
            "\u2705 Заявка на заморозку отправлена. Ждите подтверждения."
        )

    return ConversationHandler.END


async def freeze_custom_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена диалога кастомной заморозки."""
    sub_id = context.user_data.get("freeze_sub_id")
    context.user_data.pop("freeze_sub_id", None)
    context.user_data.pop("freeze_start", None)
    context.user_data.pop("freeze_is_admin", None)
    context.user_data.pop("freeze_admin_id", None)
    context.user_data.pop("freeze_client_user_id", None)

    if update.callback_query:
        await safe_answer(update.callback_query)
        await update.callback_query.edit_message_text("\u274c Создание заморозки отменено.")
    else:
        await update.message.reply_text("\u274c Создание заморозки отменено.")

    if sub_id:
        await show_admin_sub_manage(update, context, sub_id)
    return ConversationHandler.END


def _parse_freeze_date(raw: str) -> Optional[date]:
    """Парсит дату ДД.ММ.ГГГГ; None при неверном формате."""
    try:
        return datetime.strptime(raw, "%d.%m.%Y").date()
    except ValueError:
        return None


async def handle_admin_sub_reset_freezes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int
):
    """Сброс заморозок абонемента: отмена открытых и обнуление счётчика."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    sub = Subscription.get_by_id(sub_id)
    if not sub:
        await safe_answer(query, "Абонемент не найден", show_alert=True)
        return

    # Нечего сбрасывать: открытых заморозок нет и счётчик уже обнулён
    open_freeze = Freeze.get_open_for_subscription(sub_id)
    if not open_freeze and (sub.get("freeze_used") or 0) == 0:
        await safe_answer(
            query, "Сбрасывать нечего, заморозок нет.", show_alert=True
        )
        await show_admin_sub_manage(update, context, sub_id)
        return

    Freeze.cancel_open_for_subscription(sub_id)
    Subscription.reset_freeze_used(sub_id)
    logger.info("Админ %s сбросил заморозки абонемента %s", admin["id"], sub_id)

    await show_admin_sub_manage(
        update,
        context,
        sub_id,
        notice="✅ Все заморозки отменены.",
    )


async def handle_admin_sub_reset_manual(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int
):
    """Сброс ручных корректировок абонемента: восстановление исходного срока."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    if not Subscription.reset_manual_adjustments(sub_id, admin["id"]):
        await safe_answer(query, "Нет ручных корректировок для сброса", show_alert=True)
        await show_admin_sub_manage(update, context, sub_id)
        return

    logger.info("Админ %s сбросил ручные корректировки абонемента %s", admin["id"], sub_id)
    await safe_answer(
        query, "\u2705 Ручные корректировки сброшены. Срок действия восстановлен.", show_alert=True
    )
    await show_admin_sub_manage(update, context, sub_id)


async def handle_admin_sub_unfreeze(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int
):
    """Админ снимает заморозку досрочно: завершает и продлевает срок абонемента."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    freeze = Freeze.get_active_for_subscription(sub_id)
    if not freeze:
        await safe_answer(
            query, "Нет активной заморозки для снятия.", show_alert=True
        )
        return

    # Завершаем заморозку единой функцией: корректировка срока + сброс freeze_used
    Freeze.complete_and_revert(freeze["id"], date.today())
    logger.info("Админ %s снял заморозку %s", admin["id"], freeze["id"])

    # Уведомляем клиента
    sub = Subscription.get_by_id(sub_id)
    client = User.get_by_id(sub["user_id"]) if sub and sub.get("user_id") else None
    if client and client.get("telegram_id"):
        try:
            await context.bot.send_message(
                chat_id=client["telegram_id"],
                text="\U0001f525 Заморозка снята досрочно.",
            )
        except Exception as e:
            logger.error(
                "Не удалось уведомить клиента %s: %s", client["telegram_id"], e
            )

    await show_admin_sub_manage(
        update, context, sub_id, notice="\U0001f525 Заморозка снята досрочно"
    )


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
    """Занятия преподавателя на сегодня из events_cache (по teacher_id)."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return
    query = update.callback_query

    events = EventCache.get_by_teacher_today(teacher["id"])

    if not events:
        text = "\U0001f4c5 Мои занятия сегодня\n\nУ вас нет занятий сегодня."
        keyboard = [
            [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_panel")]
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))
        return

    lines = ["\U0001f4c5 Мои занятия сегодня", ""]
    keyboard = []
    for ev in events:
        time_str = _format_time(ev.get("start_time"))
        name = ev.get("name") or "Занятие"
        location = ev.get("location_name") or ev.get("location_id") or "—"
        em_id = ev.get("em_event_id")
        count = Booking.count_confirmed_for_event(em_id) if em_id else 0

        lines.append(f"\U0001f555 {time_str} — {name} (Зал {location})")
        lines.append(f"Записано: {count} чел.")
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f465 Список группы",
                    callback_data=f"teacher_class_{em_id}",
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_panel")]
    )
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard))


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
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_teacher_class(
    update: Update, context: ContextTypes.DEFAULT_TYPE, em_event_id: int
):
    """Список записавшихся на занятие с кнопками отметки."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return
    query = update.callback_query

    bookings = Booking.get_by_event(em_event_id) if em_event_id else []
    ev = EventCache.get_by_id(em_event_id) if em_event_id else None  # нет, em_event_id != PK

    time_str = ""
    ev_name = "Занятие"
    if em_event_id:
        # Достаём событие из кэша по em_event_id
        for e in EventCache.get_by_teacher_today(teacher["id"]):
            if e.get("em_event_id") == em_event_id:
                time_str = _format_time(e.get("start_time"))
                ev_name = e.get("name") or "Занятие"
                break

    if not bookings:
        text = f"\U0001f465 Группа на {time_str or ev_name}\n\nНет записей."
        keyboard = [
            [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_today")]
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))
        return

    lines = [
        f"\U0001f465 Группа на {time_str or ev_name} (Записано: {len(bookings)})",
        "",
    ]
    keyboard = []
    for i, b in enumerate(bookings, 1):
        who = b.get("full_name") or b.get("username") or f"ID {b.get('user_id')}"
        lines.append(f"{i}. {who}")
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"\u2705 {who}",
                    callback_data=f"teacher_mark_{b['id']}",
                )
            ]
        )

    text = "\n".join(lines)
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_today")]
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def handle_teacher_mark(
    update: Update, context: ContextTypes.DEFAULT_TYPE, booking_id: int
):
    """Отметить посещение из списка группы (преподаватель)."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return
    query = update.callback_query

    # Получаем booking
    booking = Booking.get_by_id(booking_id)
    if not booking or booking["status"] not in ("registered", "confirmed"):
        await safe_answer(query, "Запись уже обработана", show_alert=True)
        return

    em_event_id = booking.get("em_event_id")
    user_id = booking.get("user_id")

    # Находим активный абонемент клиента
    sub = Subscription.get_active_for_user(user_id)
    if not sub:
        await safe_answer(query, "У клиента нет активного абонемента", show_alert=True)
        return

    # Списание часов (по умолчанию 1.0, но можно 1.5 если в событии длительность)
    hours = 1.0
    if em_event_id:
        for e in EventCache.get_by_teacher_today(teacher["id"]):
            if e.get("em_event_id") == em_event_id:
                if e.get("end_time") and e.get("start_time"):
                    try:
                        dur = (
                            datetime.combine(date.min, e["end_time"]) 
                            - datetime.combine(date.min, e["start_time"])
                        ).total_seconds() / 3600
                        hours = round(dur, 1)
                    except Exception:
                        pass
                break

    if not Subscription.update_hours(sub["id"], -hours):
        await safe_answer(query, "Не удалось списать часы", show_alert=True)
        return

    # Меняем статус booking на attended
    Booking.update(booking_id, status="attended")

    # Создаём visit
    Visit.create(
        user_id=user_id,
        em_event_id=em_event_id or 0,
        subscription_id=sub["id"],
        marked_at=datetime.now(),
        marked_by=teacher["id"],
        method="manual_teacher",
        visit_type="subscription",
        status="attended",
        hours_charged=hours,
    )

    logger.info(
        "Преподаватель %s отметил посещение booking %s: %sч",
        teacher["id"], booking_id, hours,
    )

    await safe_answer(query, f"\u2705 Посещение отмечено ({hours}ч)", show_alert=True)
    # Возвращаемся к списку группы
    if em_event_id:
        await show_teacher_class(update, context, em_event_id)
    else:
        await show_teacher_today(update, context)


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

    # Клиент: быстрая заморозка (без sub_id — берём активный абонемент)
    if data == "freeze_quick":
        await handle_freeze_quick(update, context)
        return

    # Клиент: кастомная заморозка (без sub_id — ConversationHandler)
    if data == "freeze_custom":
        await freeze_custom_start(update, context)
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

    # --- Админка: клиенты с пагинацией ---
    if data == "admin_clients":
        await show_admin_clients(update, context)
        return

    if data.startswith("admin_clients_page_"):
        # Формат: admin_clients_page_{page}_{filter}
        parts = data[len("admin_clients_page_"):].split("_", 1)
        try:
            page = int(parts[0])
        except (ValueError, IndexError):
            page = 1
        if page < 1:
            # Некорректный номер страницы — сбрасываем на первую
            page = 1
        status_filter = parts[1] if len(parts) == 2 and parts[1] in ("new", "active", "all") else "all"
        await show_admin_clients(update, context, page=page, status_filter=status_filter)
        return

    if data == "admin_teachers":
        await show_admin_teachers(update, context)
        return

    if data == "admin_admins":
        await show_admin_admins(update, context)
        return

    # --- Админка: слоты ---
    if data == "admin_slots":
        await show_admin_slots(update, context)
        return

    if data.startswith("admin_slots_page_"):
        try:
            page = int(data[len("admin_slots_page_"):])
        except ValueError:
            page = 1
        if page < 1:
            page = 1
        await show_admin_slots(update, context, page=page)
        return

    if data.startswith("admin_slot_manage_"):
        sid = _extract_int(data, "admin_slot_manage_")
        if sid is not None:
            await show_admin_slot_manage(update, context, sid)
            return

    if data.startswith("admin_slot_assign_"):
        sid = _extract_int(data, "admin_slot_assign_")
        if sid is not None:
            await show_admin_slot_assign(update, context, sid)
            return

    if data.startswith("admin_slot_set_teacher_"):
        # Формат: admin_slot_set_teacher_{slot_id}_{teacher_id}
        parts = data[len("admin_slot_set_teacher_"):].split("_", 1)
        if len(parts) == 2:
            try:
                sid = int(parts[0])
                tid = int(parts[1])
                await handle_admin_slot_set_teacher(update, context, sid, tid)
                return
            except ValueError:
                pass

    if data == "admin_slot_create":
        await show_admin_slot_create_stub(update, context)
        return

    # Заглушка для неинтерактивных кнопок-индикаторов
    if data == "noop":
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

    # Быстрое изменение часов: admin_sub_add_hours_{sub_id}_{hours}
    if data.startswith("admin_sub_add_hours_"):
        parsed = _parse_sub_hours(data, "admin_sub_add_hours_")
        if parsed:
            sid, hours = parsed
            await handle_sub_add_hours(update, context, sid, hours)
            return

    if data.startswith("admin_sub_sub_hours_"):
        parsed = _parse_sub_hours(data, "admin_sub_sub_hours_")
        if parsed:
            sid, hours = parsed
            await handle_sub_sub_hours(update, context, sid, hours)
            return

    # Изменение срока: admin_sub_add_days_{sub_id}_{delta} (delta может быть отрицательным)
    if data.startswith("admin_sub_add_days_"):
        parsed = _extract_two_ints(data, "admin_sub_add_days_")
        if parsed:
            sid, delta = parsed
            await handle_admin_sub_add_days(update, context, sid, delta)
            return

    # Админ: быстрая заморозка (freeze_quick_{sub_id})
    if data.startswith("freeze_quick_"):
        sid = _extract_int(data, "freeze_quick_")
        if sid is not None:
            await handle_freeze_quick(update, context, sid)
            return

    # Админ: кастомная заморозка (freeze_custom_{sub_id} — ConversationHandler)
    if data.startswith("freeze_custom_"):
        sid = _extract_int(data, "freeze_custom_")
        if sid is not None:
            await freeze_custom_start(update, context)
            return

    if data.startswith("admin_sub_unfreeze_"):
        sid = _extract_int(data, "admin_sub_unfreeze_")
        if sid is not None:
            await handle_admin_sub_unfreeze(update, context, sid)
            return

    if data.startswith("admin_sub_reset_freezes_"):
        sid = _extract_int(data, "admin_sub_reset_freezes_")
        if sid is not None:
            await handle_admin_sub_reset_freezes(update, context, sid)
            return

    if data.startswith("admin_sub_reset_manual_"):
        sid = _extract_int(data, "admin_sub_reset_manual_")
        if sid is not None:
            await handle_admin_sub_reset_manual(update, context, sid)
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

    if data.startswith("teacher_class_"):
        eid = _extract_int(data, "teacher_class_")
        if eid is not None:
            await show_teacher_class(update, context, eid)
            return

    if data.startswith("teacher_mark_"):
        bid = _extract_int(data, "teacher_mark_")
        if bid is not None:
            await handle_teacher_mark(update, context, bid)
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

    # ConversationHandler кастомной заморозки (даты), единый для админа и клиента
    freeze_custom_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(freeze_custom_start, pattern=r"^freeze_custom$"),
            CallbackQueryHandler(freeze_custom_start, pattern=r"^freeze_custom_\d+$"),
        ],
        states={
            ADMIN_FREEZE_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, freeze_custom_start_date)
            ],
            ADMIN_FREEZE_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, freeze_custom_end_date)
            ],
        },
        fallbacks=[CommandHandler("cancel", freeze_custom_cancel)],
    )

    # ConversationHandler редактирования профиля (супер-админ)
    admin_profile_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_profile_start, pattern=r"^admin_profile_edit_\d+$")
        ],
        states={
            ADMIN_PROFILE_FIELD: [
                CallbackQueryHandler(admin_profile_choose_field, pattern=r"^admin_profile_field_"),
                CallbackQueryHandler(admin_profile_cancel, pattern=r"^admin_profile_cancel$"),
            ],
            ADMIN_PROFILE_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_profile_value),
                CallbackQueryHandler(admin_profile_cancel, pattern=r"^admin_profile_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_profile_cancel)],
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
    application.add_handler(freeze_custom_conv)
    application.add_handler(admin_profile_conv)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    application.add_handler(CallbackQueryHandler(button_handler))

    # Запуск в режиме polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
