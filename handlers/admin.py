"""
Админ-хендлеры: панель администратора, клиенты, слоты, роли,
абонементы и заморозки.
"""
import json
import logging
import math
import re
from datetime import date, datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from db.connection import get_connection
from db.models import (
    AuditLog,
    Booking,
    Freeze,
    Group,
    Subscription,
    SubscriptionType,
    User,
    UserRole,
    Visit,
)
from handlers.helpers import (
    ADMIN_PROFILE_FIELDS,
    ADMIN_SUB_STATUS_LABELS,
    CLIENTS_PER_PAGE,
    FREEZE_MAX_DAYS,
    SLOTS_PER_PAGE,
    USER_STATUS_EMOJI,
    _client_button_label,
    _extract_int,
    _extract_two_ints,
    _format_date,
    _format_time,
    _format_user_line,
    _get_user_and_active_sub,
    _parse_freeze_date,
    _parse_sub_hours,
    _plural_days,
    _require_admin,
    _safe_edit,
    _short_name,
    check_and_complete_freeze,
    notify_admin,
    safe_answer,
)

logger = logging.getLogger(__name__)

# Состояние диалога смены даты окончания абонемента (админка)
ADMIN_SUB_EXPIRES_DATE = 10

# Состояния диалога создания заморозки админом (даты начала/окончания)
ADMIN_FREEZE_START, ADMIN_FREEZE_END = 11, 12

# Состояния диалога редактирования профиля (супер-админ)
ADMIN_PROFILE_FIELD, ADMIN_PROFILE_VALUE = 13, 14

# Состояния диалога создания слота
ASK_SLOT_NAME, ASK_SLOT_WEEKDAYS, ASK_SLOT_TIME_START, ASK_SLOT_TIME_END = 15, 16, 17, 18


# ----------------------------------------------------------------------
# Админ-панель
# ----------------------------------------------------------------------


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
            [InlineKeyboardButton("\u2795 Создать слот", callback_data="admin_slot_create")],
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
        [InlineKeyboardButton("\u2795 Создать слот", callback_data="admin_slot_create")]
    )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")]
    )
    if query:
        await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard)
        )


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


async def admin_slot_create_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Шаг 1 — создание слота: запрос названия."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END
    query = update.callback_query

    context.user_data["slot_new"] = {}
    await query.edit_message_text(
        "\u2795 Создание слота\n\n"
        "Введите название слота (например: Бачата Пн/Ср 18:00):"
    )
    return ASK_SLOT_NAME


async def admin_slot_ask_weekdays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2 — валидация названия, запрос дней недели."""
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text(
            "\u26a0\ufe0f Название слишком короткое. Введите ещё раз:"
        )
        return ASK_SLOT_NAME

    context.user_data.setdefault("slot_new", {})["name"] = name
    await update.message.reply_text(
        "Введите дни недели цифрами через запятую (1=Пн, 7=Вс, например: 1,3):"
    )
    return ASK_SLOT_WEEKDAYS


async def admin_slot_ask_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3 — валидация дней недели, запрос времени начала."""
    raw = update.message.text.strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts or not all(p.isdigit() and 1 <= int(p) <= 7 for p in parts):
        await update.message.reply_text(
            "\u26a0\ufe0f Неверный формат. Введите дни недели цифрами 1-7 "
            "через запятую (например: 1,3):"
        )
        return ASK_SLOT_WEEKDAYS

    context.user_data.setdefault("slot_new", {})["weekdays"] = ",".join(parts)
    await update.message.reply_text(
        "Введите время начала (ЧЧ:ММ, например 18:00):"
    )
    return ASK_SLOT_TIME_START


async def admin_slot_ask_time_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 4 — валидация времени начала, запрос времени окончания."""
    raw = update.message.text.strip()
    try:
        start_dt = datetime.strptime(raw, "%H:%M")
    except ValueError:
        await update.message.reply_text(
            "\u26a0\ufe0f Неверный формат времени. Введите ЧЧ:ММ (например 18:00):"
        )
        return ASK_SLOT_TIME_START

    context.user_data.setdefault("slot_new", {})["time_start"] = start_dt.strftime(
        "%H:%M:%S"
    )
    await update.message.reply_text(
        "Введите время окончания (ЧЧ:ММ, например 19:30):"
    )
    return ASK_SLOT_TIME_END


async def admin_slot_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 5 — валидация времени окончания, сохранение слота."""
    raw = update.message.text.strip()
    try:
        end_dt = datetime.strptime(raw, "%H:%M")
    except ValueError:
        await update.message.reply_text(
            "\u26a0\ufe0f Неверный формат времени. Введите ЧЧ:ММ (например 19:30):"
        )
        return ASK_SLOT_TIME_END

    data = context.user_data.get("slot_new") or {}
    start_str = data.get("time_start")
    if not start_str:
        await update.message.reply_text("Диалог устарел. Начните заново.")
        return ConversationHandler.END

    start_dt = datetime.strptime(start_str, "%H:%M:%S")
    if end_dt <= start_dt:
        await update.message.reply_text(
            "\u26a0\ufe0f Время окончания должно быть позже времени начала. "
            "Введите ещё раз:"
        )
        return ASK_SLOT_TIME_END

    name = data.get("name") or "Слот"
    weekdays = data.get("weekdays") or ""
    # category_slug — первое слово названия в нижнем регистре; fallback 'dance'
    first_word = name.split()[0] if name.split() else ""
    category_slug = (first_word.lower()[:50]) or "dance"

    # Время в формате HH:MM:00
    time_start_str = start_dt.strftime("%H:%M:00")
    time_end_str = end_dt.strftime("%H:%M:00")

    try:
        Group.create_with_duration(
            name=name,
            category_slug=category_slug,
            weekdays=weekdays,
            time_start=time_start_str,
            time_end=time_end_str,
            active=1,
        )
        logger.info(
            "Админ создал слот: %s (%s, %s-%s)",
            name,
            weekdays,
            time_start_str,
            time_end_str,
        )
        await update.message.reply_text(f"\u2705 Слот '{name}' успешно создан!")
    except Exception as e:
        logger.error(
            "Ошибка при создании слота: %s | данные: name=%r, category_slug=%r, "
            "weekdays=%r, time_start=%r, time_end=%r",
            e,
            name,
            category_slug,
            weekdays,
            time_start_str,
            time_end_str,
        )
        await update.message.reply_text(
            "\u26a0\ufe0f Не удалось создать слот. Попробуйте позже."
        )

    context.user_data.pop("slot_new", None)
    await show_admin_slots(update, context)
    return ConversationHandler.END


async def admin_slot_create_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Отмена создания слота."""
    context.user_data.pop("slot_new", None)
    if update.callback_query:
        await safe_answer(update.callback_query)
        await update.callback_query.edit_message_text("\u274c Создание слота отменено.")
    else:
        await update.message.reply_text("\u274c Создание слота отменено.")
    await show_admin_slots(update, context)
    return ConversationHandler.END


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
        f"Email: {u.get('email') or '—'}\n"
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
        [
            InlineKeyboardButton(
                "\U0001f4e7 Email", callback_data=f"admin_user_email_{user_id}"
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


# ----------------------------------------------------------------------
# Редактирование email пользователя (для привязки преподавателей)
# ----------------------------------------------------------------------
ADMIN_EMAIL_EDIT = 19


async def admin_email_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрос нового email для пользователя."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END
    query = update.callback_query

    try:
        user_id = int(query.data.replace("admin_user_email_", ""))
    except (ValueError, AttributeError):
        await query.edit_message_text("Ошибка: неверный ID пользователя.")
        return ConversationHandler.END

    target = User.get_by_id(user_id)
    if not target:
        await safe_answer(query, "Пользователь не найден", show_alert=True)
        return ConversationHandler.END

    context.user_data["admin_email_user_id"] = user_id
    text = (
        f"\U0001f4e7 Изменение Email\n\n"
        f"Пользователь: {target.get('full_name') or target.get('username') or user_id}\n"
        f"Текущий email: {target.get('email') or '—'}\n\n"
        "Введите новый email:"
    )
    keyboard = [[InlineKeyboardButton("\U0001f519 Отмена", callback_data="admin_email_cancel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_EMAIL_EDIT


async def admin_email_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Валидация и сохранение нового email."""
    value = update.message.text.strip()
    if not re.match(r"^[^@\s]+@[^@\s]+$", value):
        await update.message.reply_text(
            "\u26a0\ufe0f Неверный email. Введите ещё раз:"
        )
        return ADMIN_EMAIL_EDIT

    user_id = context.user_data.get("admin_email_user_id")
    context.user_data.pop("admin_email_user_id", None)
    if not user_id:
        await update.message.reply_text("Диалог устарел. Откройте карточку заново.")
        return ConversationHandler.END

    User.update(user_id, email=value)
    logger.info(
        "Админ (telegram %s) изменил email пользователя %s на %s",
        update.effective_user.id,
        user_id,
        value,
    )
    await update.message.reply_text(f"\u2705 Email обновлён: {value}")
    await show_admin_user_card(update, context, user_id)
    return ConversationHandler.END


async def admin_email_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена редактирования email."""
    user_id = context.user_data.get("admin_email_user_id")
    context.user_data.pop("admin_email_user_id", None)

    if update.callback_query:
        await safe_answer(update.callback_query)
        await update.callback_query.edit_message_text(
            "\U0001f4e7 Изменение email отменено."
        )
    else:
        await update.message.reply_text("\U0001f4e7 Изменение email отменено.")

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


# ----------------------------------------------------------------------
# Заморозки: одобрение заявок, быстрая и кастомная заморозка
# ----------------------------------------------------------------------


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
