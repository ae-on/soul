"""
Хендлеры панели преподавателя: занятия, группы, отметка посещений.
Полностью работают на events_cache (расписание с сайта).
"""
import logging
from datetime import date, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db.models import Booking, EventCache, Subscription, User, Visit
from handlers.helpers import (
    _format_date,
    _format_time,
    _require_teacher,
    _safe_edit,
    safe_answer,
)

logger = logging.getLogger(__name__)


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