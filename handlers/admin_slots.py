"""
Хендлеры управления слотами (группами): список, управление, автосбор из расписания.
"""
import logging
import math
from datetime import date, datetime
from typing import Optional

from pymysql.cursors import DictCursor
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


def _slot_location_icon(location_id) -> str:
    """Иконка зала: 🏠 для Зал 1, 🏡 для Зал 2, иначе 🏢."""
    if location_id == 1:
        return "\U0001f3e0"
    if location_id == 2:
        return "\U0001f3e1"
    return "\U0001f3e2"


def _slot_sort_key(slot: dict):
    """Сортировка: первый день недели (Пн=1), затем time_start, затем name."""
    days = [int(d) for d in (slot.get("weekdays") or "").split(",") if d.strip()]
    first_day = min(days) if days else 99
    return (
        first_day,
        str(slot.get("time_start") or ""),
        (slot.get("name") or "").lower(),
    )


def _slot_button_label(slot: dict) -> str:
    """
    Кнопка слота: «🏠 Пн/Ср 18:00 Бачата Иван».
    В конце — неразрывные пробелы (\u00A0) для имитации левого края.
    Это костыль: Telegram центрирует текст кнопок, другого способа нет.
    """
    # location_id может приходить строкой — приводим к int
    raw_loc = slot.get("location_id")
    try:
        location_id = int(raw_loc) if raw_loc is not None else None
    except (ValueError, TypeError):
        location_id = None
    icon = _slot_location_icon(location_id)

    days_str = _format_weekdays(slot.get("weekdays"))
    time_str = _format_time(slot.get("time_start"))
    name = slot.get("name") or slot.get("category_slug") or "—"
    name = name if len(name) <= 10 else name[:9] + "…"
    teacher_name = slot.get("teacher_name") or ""
    if teacher_name:
        surname = teacher_name.split()[0]
        teacher = surname if len(surname) <= 5 else surname[:5]
    else:
        teacher = "---"
    pad = "\u00A0" * 8
    return f"{icon} {days_str} {time_str} {name} {teacher}{pad}"


def _slot_future_events_count(slot: dict) -> int:
    """Количество будущих событий в events_cache, соответствующих слоту."""
    with get_connection() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                """SELECT COUNT(*) AS cnt FROM events_cache e
                   WHERE e.start_date >= CURDATE()
                     AND e.start_time = %s
                     AND FIND_IN_SET(WEEKDAY(e.start_date) + 1, %s) > 0
                     AND COALESCE(e.location_id, 0) = COALESCE(%s, 0)
                     AND COALESCE(e.category_slug, '') = %s""",
                (
                    str(slot.get("time_start") or ""),
                    slot.get("weekdays") or "",
                    slot.get("location_id"),
                    slot.get("category_slug") or "",
                ),
            )
            row = cur.fetchone()
            return row["cnt"] if row else 0


def _slot_future_bookings_count(slot: dict) -> int:
    """Количество активных записей на будущие события, соответствующих слоту."""
    with get_connection() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                """SELECT COUNT(*) AS cnt FROM bookings b
                   JOIN events_cache e ON e.em_event_id = b.em_event_id
                   WHERE e.start_date >= CURDATE()
                     AND e.start_time = %s
                     AND FIND_IN_SET(WEEKDAY(e.start_date) + 1, %s) > 0
                     AND COALESCE(e.location_id, 0) = COALESCE(%s, 0)
                     AND COALESCE(e.category_slug, '') = %s
                     AND b.status IN ('registered', 'pending', 'confirmed')""",
                (
                    str(slot.get("time_start") or ""),
                    slot.get("weekdays") or "",
                    slot.get("location_id"),
                    slot.get("category_slug") or "",
                ),
            )
            row = cur.fetchone()
            return row["cnt"] if row else 0


# ----------------------------------------------------------------------
# Список слотов (Расписание занятий)
# ----------------------------------------------------------------------
async def show_admin_slots(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1
):
    """Список слотов кнопками, отсортированных по weekdays/time_start/name."""
    admin = await _require_admin(update, context)
    if not admin:
        return

    # Все слоты с преподавателями, сортировка в Python
    # (get_all_with_teachers возвращает кортеж — используем sorted, а не .sort)
    slots = sorted(Group.get_all_with_teachers(limit=1000, offset=0), key=_slot_sort_key)

    if not slots:
        text = (
            "\U0001f4c5 Расписание пусто. Нажмите «Собрать слоты», "
            "чтобы загрузить данные с сайта."
        )
        keyboard = [
            [InlineKeyboardButton("\U0001f504 Собрать слоты", callback_data="admin_slots_sync")],
            [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_panel")],
        ]
        await _safe_edit(update, text, InlineKeyboardMarkup(keyboard))
        return

    total = len(slots)
    total_pages = max(1, math.ceil(total / SLOTS_PER_PAGE))
    page = max(1, min(page, total_pages))
    page_slots = slots[(page - 1) * SLOTS_PER_PAGE : page * SLOTS_PER_PAGE]

    text = f"\U0001f4c5 Расписание занятий (Всего: {total})"
    keyboard = []
    for slot in page_slots:
        keyboard.append(
            [
                InlineKeyboardButton(
                    _slot_button_label(slot),
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

    keyboard.append(
        [
            InlineKeyboardButton("\U0001f504 Собрать слоты", callback_data="admin_slots_sync"),
            InlineKeyboardButton("\U0001f5d1\ufe0f Удалить слоты", callback_data="admin_slots_delete_menu"),
        ]
    )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")]
    )
    await _safe_edit(update, text, InlineKeyboardMarkup(keyboard))


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
    """Автосбор слотов из events_cache.

    Ключ сопоставления: f"{category_slug}_{time_start}_{time_end}_{location_id}_{weekday}".
    Каждый слот соответствует одному дню недели (weekdays = "N").
    У существующего слота обновляется только name; teacher_id не трогаем.
    Активные слоты без будущих событий переводятся в архив (active=0).
    """
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

    # События: самое свежее первым (для выбора актуального name)
    with get_connection() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                """SELECT name, start_time, end_time, start_date,
                          location_id, category_slug, teacher_id
                   FROM events_cache
                   WHERE name IS NOT NULL AND name != ''
                   ORDER BY start_date DESC, start_time"""
            )
            rows = cur.fetchall()

    # Группируем по (name, time_start, time_end, location_id), собирая дни недели
    groups: dict[tuple, dict] = {}
    for r in rows:
        weekday = _event_weekday(r["start_date"])
        if not weekday:
            continue
        key = (
            r["name"],
            str(r["start_time"]),
            str(r["end_time"]) if r["end_time"] else "",
            r["location_id"],
        )
        g = groups.setdefault(
            key,
            {"days": set(), "category_slug": r["category_slug"] or "dance", "teacher_id": None},
        )
        g["days"].add(int(weekday))
        if not g["teacher_id"] and r["teacher_id"]:
            g["teacher_id"] = r["teacher_id"]

    created = 0
    updated = 0
    for (name, time_start, time_end, location_id), g in groups.items():
        weekdays = ",".join(str(d) for d in sorted(g["days"]))
        category_slug = g["category_slug"]
        teacher_id = g["teacher_id"]

        # Ищем слот с таким же ключом (name, time_start, time_end, location_id)
        with get_connection() as conn:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """SELECT id, teacher_id FROM slots
                       WHERE name = %s AND time_start = %s
                         AND COALESCE(time_end, '') = %s
                         AND COALESCE(location_id, 0) = %s
                       LIMIT 1""",
                    (name, time_start, time_end, location_id or 0),
                )
                existing = cur.fetchone()

        if existing:
            # Обновляем name и weekdays; teacher_id — только если у слота его нет
            updates = {"name": name, "weekdays": weekdays}
            if not existing.get("teacher_id") and teacher_id:
                updates["teacher_id"] = teacher_id
            try:
                Group.update(existing["id"], **updates)
                updated += 1
            except Exception as e:
                logger.error("Ошибка при обновлении слота %s: %s", existing["id"], e)
            continue

        # Создаём новый слот (weekdays = "1,3")
        try:
            Group.create_with_duration(
                name=name,
                category_slug=category_slug,
                weekdays=weekdays,
                time_start=time_start,
                time_end=time_end,
                location_id=location_id,
                teacher_id=teacher_id,
                active=1,
            )
            created += 1
        except Exception as e:
            logger.error("Ошибка при создании слота %s: %s", name, e)

    # Архивация активных слотов без будущих событий
    archived = 0
    with get_connection() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute("SELECT * FROM slots WHERE active = 1")
            active_slots = cur.fetchall()
    for slot in active_slots:
        if _slot_future_events_count(slot) == 0:
            Group.update(slot["id"], active=0)
            archived += 1

    logger.info(
        "Автосбор слотов: событий %d, создано %d, обновлено %d, в архив %d",
        total, created, updated, archived,
    )
    await safe_answer(
        query,
        f"Событий: {total}. Создано: {created}, обновлено: {updated}, в архив: {archived}",
        show_alert=True,
    )
    await show_admin_slots(update, context)


# ----------------------------------------------------------------------
# Удаление слотов и архива
# ----------------------------------------------------------------------
async def show_admin_slots_delete_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Меню удаления: архивные слоты + очистка пустых."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    with get_connection() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute("SELECT * FROM slots WHERE active = 0 ORDER BY name")
            archived = cur.fetchall()

    if not archived:
        text = "\U0001f5d1\ufe0f Удаление слотов\n\nВ архиве нет слотов."
    else:
        lines = [f"\U0001f5d1\ufe0f Удаление слотов (в архиве: {len(archived)})", ""]
        for slot in archived:
            label = (
                f"{slot.get('name') or slot.get('category_slug') or '—'} "
                f"{_format_weekdays(slot.get('weekdays'))} "
                f"{_format_time(slot.get('time_start'))}"
            )
            lines.append(f"• {label}")
        text = "\n".join(lines)

    keyboard = []
    for slot in archived:
        label = (
            f"{slot.get('name') or slot.get('category_slug') or '—'} "
            f"{_format_weekdays(slot.get('weekdays'))} "
            f"{_format_time(slot.get('time_start'))}"
        )
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"admin_slot_delete_do_{slot['id']}")]
        )
    keyboard.append(
        [InlineKeyboardButton("\U0001f9f9 Очистить без привязок", callback_data="admin_slots_clear_empty")]
    )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_slots")]
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def handle_admin_slots_clear_empty(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Удалить слоты без преподавателя и без записей на будущие события."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    with get_connection() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute("SELECT * FROM slots WHERE teacher_id IS NULL")
            candidates = cur.fetchall()

    deleted = 0
    for slot in candidates:
        if _slot_future_bookings_count(slot) == 0:
            try:
                Group.delete(slot["id"])
                deleted += 1
            except Exception as e:
                logger.error("Ошибка при удалении слота %s: %s", slot["id"], e)

    logger.info("Админ %s очистил слоты без привязок: %s", admin["id"], deleted)
    await safe_answer(query, f"Удалено слотов: {deleted}", show_alert=True)
    await show_admin_slots_delete_menu(update, context)


async def handle_admin_slot_delete_do(
    update: Update, context: ContextTypes.DEFAULT_TYPE, slot_id: int
):
    """Удалить архивный слот, если нет записей на будущие события."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    slot = Group.get_by_id(slot_id)
    if not slot:
        await safe_answer(query, "Слот не найден", show_alert=True)
        await show_admin_slots_delete_menu(update, context)
        return

    bookings = _slot_future_bookings_count(slot)
    if bookings > 0:
        await safe_answer(
            query,
            "Нельзя удалить: к слоту привязаны записи!",
            show_alert=True,
        )
        await show_admin_slots_delete_menu(update, context)
        return

    try:
        Group.delete(slot_id)
        logger.info("Админ %s удалил слот %s", admin["id"], slot_id)
        await safe_answer(query, "\U0001f5d1\ufe0f Слот удалён", show_alert=True)
    except Exception as e:
        logger.error("Ошибка при удалении слота %s: %s", slot_id, e)
        await safe_answer(query, "Ошибка при удалении", show_alert=True)

    await show_admin_slots_delete_menu(update, context)


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
    ]

    # «Привязать данные» — только если слот пустой (нет будущих событий или преподавателя)
    if _slot_future_events_count(slot) == 0 or not slot.get("teacher_id"):
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f517 Привязать данные",
                    callback_data=f"admin_slot_link_{slot_id}",
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "\U0001f5d1\ufe0f Удалить класс",
                callback_data=f"admin_slot_delete_{slot_id}",
            )
        ]
    )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 К списку слотов", callback_data="admin_slots")]
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_admin_slot_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE, slot_id: int
):
    """Список слотов для переноса данных (слияние)."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    slot = Group.get_by_id(slot_id)
    if not slot:
        await safe_answer(query, "Слот не найден", show_alert=True)
        return

    # Другие слоты, отсортированные по name
    others = [
        s for s in Group.get_all_with_teachers(limit=1000, offset=0)
        if s["id"] != slot_id
    ]
    others.sort(key=lambda s: (s.get("name") or "").lower())

    text = (
        f"\U0001f517 Привязать данные\n\n"
        f"Слот: {slot.get('name') or slot.get('category_slug') or '—'}\n"
        "Выберите слот, на который перенести преподавателя:"
    )
    keyboard = []
    for s in others:
        label = s.get("name") or s.get("category_slug") or f"Слот #{s['id']}"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{label} | {_format_weekdays(s.get('weekdays'))} | {_format_time(s.get('time_start'))}",
                    callback_data=f"admin_slot_link_do_{s['id']}_{slot_id}",
                )
            ]
        )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data=f"admin_slot_manage_{slot_id}")]
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def handle_admin_slot_link_do(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    new_slot_id: int,
    old_slot_id: int,
):
    """Перенос данных со старого слота на новый, старый — в архив."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    old_slot = Group.get_by_id(old_slot_id)
    new_slot = Group.get_by_id(new_slot_id)
    if not old_slot or not new_slot:
        await safe_answer(query, "Слот не найден", show_alert=True)
        return

    # Переносим привязки со старого на новый, не затирая данные нового
    updates = {}
    if not new_slot.get("teacher_id") and old_slot.get("teacher_id"):
        updates["teacher_id"] = old_slot["teacher_id"]
    if not new_slot.get("location_id") and old_slot.get("location_id"):
        updates["location_id"] = old_slot["location_id"]

    try:
        if updates:
            Group.update(new_slot_id, **updates)
        # Старый слот — в архив
        Group.update(old_slot_id, active=0)
        logger.info(
            "Админ %s связал слот %s -> %s (перенесено: %s), старый в архив",
            admin["id"], old_slot_id, new_slot_id, list(updates.keys()),
        )
        await safe_answer(
            query, "\u2705 Данные перенесены, старый слот в архиве", show_alert=True
        )
    except Exception as e:
        logger.error(
            "Ошибка при связывании слотов %s -> %s: %s",
            old_slot_id, new_slot_id, e, exc_info=True,
        )
        await safe_answer(query, "Ошибка при связывании", show_alert=True)

    await show_admin_slots(update, context)


async def handle_admin_slot_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE, slot_id: int
):
    """Удаление класса: проверка привязок и подтверждение."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    slot = Group.get_by_id(slot_id)
    if not slot:
        await safe_answer(query, "Слот не найден", show_alert=True)
        return

    bookings = _slot_future_bookings_count(slot)
    if bookings > 0:
        await safe_answer(
            query,
            "Нельзя удалить: к слоту привязаны записи! Сначала отправьте в архив.",
            show_alert=True,
        )
        return

    text = (
        "\U0001f5d1\ufe0f Удалить класс?\n\n"
        "Вы уверены? Это действие необратимо."
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "\u2705 Да, удалить", callback_data=f"admin_slot_delete_yes_{slot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Нет", callback_data=f"admin_slot_manage_{slot_id}"
            )
        ],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def handle_admin_slot_delete_yes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, slot_id: int
):
    """Фактическое удаление слота из БД."""
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
