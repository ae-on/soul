import asyncio
import logging
import os
import warnings
from datetime import date, datetime, timedelta

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

from database import init_db, save_user, update_user_details
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

# Состояния для диалога записи
FULL_NAME, PHONE = range(2)

# Инициализируем БД и загружаем базу знаний при старте
init_db()
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

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


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
def build_days_keyboard(selected_date: date = None) -> list[list[InlineKeyboardButton]]:
    """
    Строит ряд из 7 inline-кнопок: сегодня + 6 следующих дней.
    Дата (число месяца) — только в активной (выбранной) кнопке.
    """
    today = date.today()
    if selected_date is None:
        selected_date = today

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
                callback_data=f"schedule_day_{day.isoformat()}",
            )
        )

    return [row]


# ---------------------------------------------------------------------------
# Показ расписания на конкретный день (плоский экран, глубина 1)
# ---------------------------------------------------------------------------
async def show_schedule_day(
    update: Update, context: ContextTypes.DEFAULT_TYPE, date_str: str = None
):
    """
    Показывает расписание на один день.
    date_str: 'YYYY-MM-DD' или None (тогда сегодня).
    """
    query = update.callback_query
    await query.answer()

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
    day_events = []
    target_str = selected_date.isoformat()
    for ev in events:
        w = ev.get("when", {})
        if w.get("start_date") == target_str:
            day_events.append(ev)

    # Сортировка по времени
    day_events.sort(key=lambda ev: ev.get("when", {}).get("start_time", ""))

    # Собираем текст
    lines = ["\U0001f4c5 Расписание", ""]

    if not day_events:
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
    keyboard = build_days_keyboard(selected_date)

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

    # Главное меню
    keyboard.append(
        [InlineKeyboardButton("\U0001f3e0 Главное меню", callback_data="back_to_main")]
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
    """Пока заглушка: сообщение, что запись откроется позже."""
    query = update.callback_query
    await query.answer()

    text = (
        f"\u270d\ufe0f Запись на событие #{event_id}\n\n"
        "\u0420\u0435\u0430\u043b\u044c\u043d\u0430\u044f запись откроется в ближайшее время.\n"
        "Следите за анонсами!"
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
    await query.answer()

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
# Подменю "Направления"
# ----------------------------------------------------------------------
async def show_directions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает подменю со всеми направлениями."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔥 Трайбл фьюжн", callback_data="tribal_info")],
        [InlineKeyboardButton("🩰 Восточный танец", callback_data="oriental_info")],
        [InlineKeyboardButton("🧘 Йога", callback_data="yoga_info")],
        [InlineKeyboardButton("💃 Бачата (для пар)", callback_data="bachata_info")],
        [InlineKeyboardButton("🌿 Цигун", callback_data="qigong_info")],
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
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_tribal_next7",
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
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_oriental_next7",
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
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_yoga_next7",
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
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_bachata_next7",
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
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_qigong_next7",
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
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_indian_next7",
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
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_historical_next7",
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
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Ближайшие 7 дней",
                callback_data="direction_schedule_pilates_next7",
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
    user = update.effective_user
    utm_source = context.args[0] if context.args else "direct"

    # Сохраняем пользователя (utm_source обновится, имя/телефон не затронутся)
    save_user(user.id, utm_source)

    # Приветствие с учётом источника
    welcome = f"Привет, {user.first_name}!"
    if utm_source == "bachata":
        welcome += "\n\nРады видеть вас на странице Бачаты! 💃"
    else:
        welcome += "\n\nДобро пожаловать в нашу студию танцев и йоги! 🧘"

    await show_main_menu(
        update, context, text=welcome + "\n\nВыберите действие в меню ниже:"
    )


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
    """Отменяет диалог и возвращает главное меню."""
    context.user_data["booking_active"] = False
    await show_main_menu(
        update, context, text="❌ Диалог отменён. Возвращаюсь в главное меню."
    )
    return ConversationHandler.END


# ----------------------------------------------------------------------
# ConversationHandler: запись на занятие
# ----------------------------------------------------------------------
async def book_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1 — запрашиваем имя."""
    context.user_data["booking_active"] = True
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "✍️ Давайте запишем вас на занятие!\n\n"
        "Шаг 1 из 2. Как вас зовут? (напишите имя и фамилию)\n\n"
        "Или нажмите /cancel чтобы отменить запись."
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
        "Или нажмите /cancel чтобы отменить запись."
    )
    return PHONE


async def book_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Финал — сохраняем телефон + имя в БД, уведомляем администратора."""
    phone = update.message.text.strip()
    if not phone:
        await update.message.reply_text("Пожалуйста, напишите ваш номер телефона.")
        return PHONE

    full_name = context.user_data.get("full_name", "")
    telegram_id = update.effective_user.id

    # Сохраняем в БД
    update_user_details(telegram_id, full_name, phone)

    # Уведомление администратору
    if ADMIN_CHAT_ID:
        admin_message = (
            f"✅ Новая запись на занятие!\n\n"
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

    # Показываем подтверждение и возвращаем главное меню
    await update.message.reply_text(
        f"✅ Вы записаны!\n\n"
        f"Имя: {full_name}\n"
        f"Телефон: {phone}\n\n"
        "Мы свяжемся с вами в ближайшее время для подтверждения."
    )
    # Сбрасываем флаг диалога
    context.user_data["booking_active"] = False
    await show_main_menu(update, context)
    return ConversationHandler.END


# ----------------------------------------------------------------------
# Обработчик инлайн-кнопок (все, кроме book, который перехватывается
# ConversationHandler)
# ----------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    # Логируем все callback_data для отладки
    logger.info("Callback: %s", data)

    # --- Расписание: плоский показ на выбранный день ---
    if data == "schedule":
        await show_schedule_day(update, context, None)
        return

    # --- Расписание: переключение дня по schedule_day_YYYY-MM-DD ---
    if data.startswith("schedule_day_"):
        date_str = data[len("schedule_day_") :]
        await show_schedule_day(update, context, date_str)
        return

    # --- Запись на событие (заглушка) ---
    # --- Расписание по направлению (direction_schedule_{slug}_next7) ---
    if data.startswith("direction_schedule_"):
        # data = "direction_schedule_bachata_next7"
        parts = data.split("_")
        # parts = ["direction", "schedule", "bachata", "next7"]
        if len(parts) >= 4:
            slug = parts[2]
            await show_direction_schedule(update, context, slug)
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

    # --- Возврат в главное меню ---
    if data == "back_to_main":
        await show_main_menu(update, context)
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

    # ConversationHandler для записи на занятие
    book_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(book_start, pattern="^book$")],
        states={
            FULL_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, book_full_name)
            ],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_phone)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
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
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    application.add_handler(CallbackQueryHandler(button_handler))

    # Запуск в режиме polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
