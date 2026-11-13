"""
Хендлеры панели преподавателя: занятия, группы, отметка посещений, клиенты.
Полностью работают на events_cache (расписание с сайта).
"""
import logging
import math
from datetime import date, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from db.models import Booking, EventCache, Subscription, User, Visit
from handlers.helpers import (
    CLIENTS_PER_PAGE,
    _extract_int,
    _format_date,
    _format_time,
    _require_teacher,
    _safe_edit,
    _short_name,
    format_client_card_info,
    safe_answer,
)

logger = logging.getLogger(__name__)

# Состояние диалога поиска клиентов (панель преподавателя)
TEACHER_CLIENTS_SEARCH = 60


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
        [
            InlineKeyboardButton(
                "\U0001f6d2 Продать абонемент", callback_data="teacher_sell"
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f465 Клиенты", callback_data="teacher_clients"
            )
        ],
        [InlineKeyboardButton("\U0001f519 Главное меню", callback_data="back_to_main")],
    ]
    await _safe_edit(
        query,
        "\U0001f9d1\u200d\U0001f3eb Панель преподавателя",
        InlineKeyboardMarkup(keyboard),
    )


async def show_teacher_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Занятия преподавателя на сегодня (из events_cache по teacher_id)."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return
    query = update.callback_query

    events = EventCache.get_by_teacher_today(teacher["id"])

    if not events:
        text = "\U0001f4c5 Мои занятия сегодня\n\nУ вас нет занятий на сегодня."
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
        em_id = ev.get("em_event_id")
        count = Booking.count_confirmed_for_event(em_id) if em_id else 0

        lines.append(f"\U0001f555 {time_str} — {name}")
        lines.append(f"Записано: {count} чел.")
        if em_id:
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
    """Расписание преподавателя на ближайшие 7 дней (группировка по дате)."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return
    query = update.callback_query

    today = date.today()
    end = today + timedelta(days=6)
    events = EventCache.get_by_teacher_period(teacher["id"], today, end)

    if not events:
        text = "\U0001f4c5 Расписание на неделю\n\nУ вас нет занятий в ближайшие 7 дней."
        keyboard = [
            [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_panel")]
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))
        return

    lines = ["\U0001f4c5 Расписание на неделю", ""]
    keyboard = []

    # Группируем события по дате
    by_date: dict = {}
    for ev in events:
        by_date.setdefault(ev.get("start_date"), []).append(ev)

    for day, day_events in sorted(by_date.items()):
        lines.append(f"— {_format_date(day)} —")
        for ev in day_events:
            time_str = _format_time(ev.get("start_time"))
            name = ev.get("name") or "Занятие"
            em_id = ev.get("em_event_id")
            count = Booking.count_confirmed_for_event(em_id) if em_id else 0

            lines.append(f"\U0001f555 {time_str} — {name} ({count} чел.)")
            if em_id:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"{time_str} — {name}",
                            callback_data=f"teacher_class_{em_id}",
                        )
                    ]
                )
        lines.append("")

    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_panel")]
    )
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard))


async def show_teacher_class(
    update: Update, context: ContextTypes.DEFAULT_TYPE, em_event_id: int
):
    """Список записавшихся на занятие с кнопками отметки (из bookings)."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return
    query = update.callback_query

    bookings = Booking.get_by_event(em_event_id) if em_event_id else []
    ev = EventCache.get_by_em_event(em_event_id) if em_event_id else None
    time_str = _format_time(ev.get("start_time")) if ev else ""
    ev_name = ev.get("name") if ev else "Занятие"

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

    # Списание часов: длительность из события в events_cache (по умолчанию 1.0)
    hours = 1.0
    if em_event_id:
        ev = EventCache.get_by_em_event(em_event_id)
        if ev and ev.get("end_time") and ev.get("start_time"):
            try:
                dur = (
                    datetime.combine(date.min, ev["end_time"])
                    - datetime.combine(date.min, ev["start_time"])
                ).total_seconds() / 3600
                hours = round(dur, 1)
            except Exception:
                pass

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
# Клиенты (панель преподавателя)
# ----------------------------------------------------------------------
async def show_teacher_clients(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1
):
    """Список клиентов с пагинацией (по 10) и поиском."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return

    total = User.count_students_for_admin("all")
    total_pages = max(1, math.ceil(total / CLIENTS_PER_PAGE))
    page = max(1, min(page, total_pages))
    users = User.get_students_for_admin(
        "all", limit=CLIENTS_PER_PAGE, offset=(page - 1) * CLIENTS_PER_PAGE
    )

    lines = [f"\U0001f465 Клиенты (Всего: {total})", ""]
    keyboard = []
    for u in users:
        name = _short_name(u.get("full_name") or u.get("username") or f"ID {u['id']}")
        lines.append(f"• {name}")
        keyboard.append(
            [
                InlineKeyboardButton(
                    name, callback_data=f"teacher_client_manage_{u['id']}"
                )
            ]
        )

    # Пагинация
    nav = []
    if page > 1:
        nav.append(
            InlineKeyboardButton(
                "\u2b05\ufe0f", callback_data=f"teacher_clients_page_{page - 1}"
            )
        )
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="teacher_clients_noop"))
    if page < total_pages:
        nav.append(
            InlineKeyboardButton(
                "\u27a1\ufe0f", callback_data=f"teacher_clients_page_{page + 1}"
            )
        )
    keyboard.append(nav)
    keyboard.append(
        [InlineKeyboardButton("\U0001f50d Поиск", callback_data="teacher_clients_search")]
    )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_panel")]
    )
    await _safe_edit(update, "\n".join(lines), InlineKeyboardMarkup(keyboard))


async def show_teacher_client_manage(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
):
    """Карточка клиента для преподавателя: ФИО, телефон, абонемент, продажа."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return

    u = User.get_by_id(user_id)
    if not u:
        await safe_answer(update.callback_query, "Клиент не найден", show_alert=True)
        return

    text = (
        f"\U0001f464 {u['full_name'] or '—'}\n"
        f"\U0001f4f1 Телефон: {u['phone'] or '—'}\n"
        f"{format_client_card_info(user_id)}"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f6d2 Продать абонемент",
                callback_data=f"teacher_sell_manage_{user_id}",
            )
        ],
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="teacher_clients")],
    ]
    await _safe_edit(update, text, InlineKeyboardMarkup(keyboard))


async def teacher_client_manage_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатия на клиента из результатов поиска (ConversationHandler)."""
    query = update.callback_query
    user_id = _extract_int(query.data, "teacher_client_manage_")
    if user_id is not None:
        await show_teacher_client_manage(update, context, user_id)
    return ConversationHandler.END


async def teacher_clients_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрос Telegram ID или ФИО клиента для поиска."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return ConversationHandler.END
    await _safe_edit(
        update,
        "\U0001f50d Введите Telegram ID клиента или ФИО\n"
        "(например: 123456789 или Иванова Анна):",
    )
    return TEACHER_CLIENTS_SEARCH


async def teacher_clients_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск клиента по Telegram ID или ФИО (текстовый ввод)."""
    teacher = await _require_teacher(update, context)
    if not teacher:
        return ConversationHandler.END
    raw = update.message.text.strip()

    # Поиск по Telegram ID
    if raw.isdigit():
        user = User.get_by_telegram_id(int(raw))
        if user:
            await show_teacher_client_manage(update, context, user["id"])
            return ConversationHandler.END
        await update.message.reply_text(
            "Пользователь с таким Telegram ID не найден. Попробуйте ещё раз или /cancel."
        )
        return TEACHER_CLIENTS_SEARCH

    # Поиск по ФИО/username (подстрока)
    students = User.get_students_for_admin(limit=200)
    needle = raw.lower()
    matches = [
        u
        for u in students
        if needle in (u.get("full_name") or "").lower()
        or needle in (u.get("username") or "").lower()
    ]
    if len(matches) == 1:
        await show_teacher_client_manage(update, context, matches[0]["id"])
        return ConversationHandler.END
    if len(matches) > 1:
        keyboard = [
            [
                InlineKeyboardButton(
                    _short_name(u.get("full_name") or u.get("username") or f"ID {u['id']}"),
                    callback_data=f"teacher_client_manage_{u['id']}",
                )
            ]
            for u in matches[:10]
        ]
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f519 Отмена", callback_data="teacher_clients_cancel"
                )
            ]
        )
        await update.message.reply_text(
            "Найдено несколько клиентов. Уточните выбор:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return TEACHER_CLIENTS_SEARCH

    await update.message.reply_text("Никого не найдено. Попробуйте ещё раз или /cancel.")
    return TEACHER_CLIENTS_SEARCH


async def teacher_clients_search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена поиска клиентов (кнопка или /cancel) — возврат к списку."""
    if update.callback_query:
        await safe_answer(update.callback_query)
    await show_teacher_clients(update, context, 1)
    return ConversationHandler.END