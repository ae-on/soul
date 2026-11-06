"""
Хендлеры управления слотами (группами): список, управление, автосбор из расписания.
"""
import logging
import math
from datetime import date, datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db.connection import get_connection
from db.models import EventCache, Group, User, UserRole
from handlers.helpers import (
    _format_time,
    _require_admin,
    _safe_edit,
    safe_answer,
)

logger = logging.getLogger(__name__)

SLOTS_PER_PAGE = 10

WEEKDAY_SHORT = {
    "1": "Пн", "2": "Вт", "3": "Ср", "4": "Чт",
    "5": "Пт", "6": "Сб", "7": "Вс",
}


def _format_weekdays(weekdays: str) -> str:
    """«1,3,5» → «Пн/Ср/Пт»."""
    return "/".join(
        WEEKDAY_SHORT.get(d.strip(), d.strip())
        for d in (weekdays or "").split(",")
        if d.strip()
    )


# ----------------------------------------------------------------------
# Список слотов
# ----------------------------------------------------------------------
async def show_admin_slots(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1
):
    """Список всех слотов с пагинацией и кнопкой автосбора."""
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
            [InlineKeyboardButton("\U0001f504 Собрать слоты из расписания", callback_data="admin_slots_sync")],
            [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")],
        ]
        if query:
            await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    lines = [f"\U0001f4c5 Слоты и группы (Всего: {total})", ""]
    keyboard = []
    for slot in slots:
        name = slot.get("name") or slot.get("category_slug") or "Слот"
        days_str = _format_weekdays(slot.get("weekdays"))
        time_str = _format_time(slot.get("time_start"))
        teacher_name = slot.get("teacher_name") or "Не назначен"
        # Формат: «Бачата Пн/Ср 18:00 (Иванов)»
        label = f"{name} {days_str} {time_str} ({teacher_name})"
        lines.append(f"• {label}")
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"\U0001f4c5 {label}",
                    callback_data=f"admin_slot_manage_{slot['id']}",
                )
            ]
        )

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
        [InlineKeyboardButton("\U0001f504 Собрать слоты из расписания", callback_data="admin_slots_sync")]
    )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")]
    )
    if query:
        await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


# ----------------------------------------------------------------------
# Автосбор слотов из events_cache
# ----------------------------------------------------------------------
def _event_weekday(start_date) -> str:
    """День недели из даты события (1=Пн...7=Вс). Работает с date и строкой."""
    if isinstance(start_date, date):
        return str(start_date.isoweekday())
    try:
        return str(datetime.strptime(str(start_date)[:10], "%Y-%m-%d").isoweekday())
    except (ValueError, TypeError):
        return ""


async def handle_admin_slots_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Автоматически создаёт слоты на основе событий в events_cache."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    # Сначала проверяем, что кэш вообще не пуст
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM events_cache")
            row = cur.fetchone()
            total = row[0] if row else 0

    if total == 0:
        await safe_answer(
            query,
            "Кэш пуст. Сначала откройте расписание, чтобы бот скачал данные с сайта.",
            show_alert=True,
        )
        return

    # Уникальные группы (name, start_time, end_time) + дата для дня недели
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT name, start_time, end_time, start_date
                   FROM events_cache
                   WHERE name IS NOT NULL AND name != ''
                   ORDER BY name, start_time"""
            )
            rows = cur.fetchall()

    # Группируем по (name, time_start, time_end), собирая дни недели
    groups: dict[tuple[str, str, str], set[str]] = {}
    for r in rows:
        key = (r[0], str(r[1]), str(r[2]) if r[2] else "")
        wd = _event_weekday(r[3])
        if wd:
            groups.setdefault(key, set()).add(wd)

    created = 0
    for (name, time_start, time_end), days in groups.items():
        weekdays = ",".join(sorted(days, key=int))
        # Проверяем, есть ли уже такой слот
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM slots WHERE name = %s AND time_start = %s LIMIT 1",
                    (name, time_start),
                )
                if cur.fetchone():
                    continue  # уже есть

        try:
            # category_slug — первое слово названия в нижнем регистре
            first_word = name.split()[0] if name.split() else "dance"
            category_slug = first_word.lower()[:50]
            Group.create_with_duration(
                name=name,
                category_slug=category_slug,
                weekdays=weekdays,
                time_start=time_start,
                time_end=time_end,
                active=1,
            )
            created += 1
        except Exception as e:
            logger.error("Ошибка при создании слота %s: %s", name, e)

    logger.info("Автосбор слотов: найдено событий %d, создано слотов %d", total, created)
    await safe_answer(
        query,
        f"Найдено событий: {total}, создано слотов: {created}",
        show_alert=True,
    )
    await show_admin_slots(update, context)


# ----------------------------------------------------------------------
# Управление слотом
# ----------------------------------------------------------------------
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
        [
            InlineKeyboardButton(
                "\U0001f5d1\ufe0f Удалить слот",
                callback_data=f"admin_slot_delete_{slot_id}",
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
    """Назначить преподавателя на слот + обновить events_cache."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    slot = Group.get_by_id(slot_id)
    if not slot:
        await safe_answer(query, "Слот не найден", show_alert=True)
        return

    try:
        Group.update(slot_id, teacher_id=teacher_id)

        # Обновляем все события в events_cache с таким же name и time_start
        name = slot.get("name") or ""
        time_start = slot.get("time_start")
        if name and time_start:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE events_cache
                           SET teacher_id = %s
                           WHERE name = %s AND start_time = %s""",
                        (teacher_id, name, time_start),
                    )
                    conn.commit()

        logger.info(
            "Админ %s назначил преподавателя %s на слот %s (events_cache обновлён)",
            admin["id"], teacher_id, slot_id,
        )
        await safe_answer(query, "\u2705 Преподаватель назначен", show_alert=True)
    except Exception as e:
        logger.error("Ошибка при назначении преподавателя: %s", e)
        await safe_answer(query, "Ошибка при назначении", show_alert=True)

    await show_admin_slot_manage(update, context, slot_id)


async def handle_admin_slot_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE, slot_id: int
):
    """Удалить слот."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    try:
        Group.delete(slot_id)
        logger.info("Админ %s удалил слот %s", admin["id"], slot_id)
        await safe_answer(query, "\U0001f5d1\ufe0f Слот удалён", show_alert=True)
    except Exception as e:
        logger.error("Ошибка при удалении слота %s: %s", slot_id, e)
        await safe_answer(query, "Ошибка при удалении", show_alert=True)

    await show_admin_slots(update, context)