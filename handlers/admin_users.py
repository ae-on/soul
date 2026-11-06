"""
Хендлеры управления пользователями: списки, карточка, CRUD, роли.
"""
import logging
import math
import re
from typing import Optional

from datetime import datetime

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
from db.constants import CONSENT_TEXT_VERSION
from db.models import User, UserRole
from handlers.helpers import (
    CLIENTS_PER_PAGE,
    _client_button_label,
    _format_user_line,
    _require_admin,
    _safe_edit,
    safe_answer,
)

logger = logging.getLogger(__name__)

ADMIN_USER_EDIT = 20

# Состояния диалога добавления пользователя админом
ADMIN_USER_ADD_NAME, ADMIN_USER_ADD_PHONE, ADMIN_USER_ADD_TG, ADMIN_USER_ADD_EMAIL = 21, 22, 23, 24

# Подписи полей, доступных для редактирования через CRUD-карточку
ADMIN_USER_FIELD_LABELS = {
    "name": "ФИО",
    "phone": "Телефон",
    "email": "Email",
}


# ----------------------------------------------------------------------
# Списки пользователей
# ----------------------------------------------------------------------
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
            [InlineKeyboardButton("\u2795 Добавить пользователя", callback_data="admin_user_add")],
            [
                InlineKeyboardButton(
                    "\U0001f5d1\ufe0f Удалить заблокированных",
                    callback_data="admin_user_delete_list",
                )
            ],
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
                    callback_data=f"admin_user_manage_{u['id']}",
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
        [InlineKeyboardButton("\u2795 Добавить пользователя", callback_data="admin_user_add")]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                "\U0001f5d1\ufe0f Удалить заблокированных",
                callback_data="admin_user_delete_list",
            )
        ]
    )
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
            [InlineKeyboardButton(f"\U0001f464 {name}", callback_data=f"admin_user_manage_{u['id']}")]
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
            [InlineKeyboardButton(f"\U0001f464 {name}", callback_data=f"admin_user_manage_{u['id']}")]
        )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")]
    )
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


# ----------------------------------------------------------------------
# Карточка пользователя
# ----------------------------------------------------------------------
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
                "\U0001f4e7 Email", callback_data=f"admin_user_edit_{user_id}_email"
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


async def show_admin_user_manage(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
):
    """CRUD-карточка пользователя: редактирование полей, роли, блокировка."""
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

    text = (
        f"\U0001f464 {u['full_name'] or '—'}\n"
        f"ID в БД: {u['id']}\n"
        f"Telegram ID: {u['telegram_id']}\n"
        f"Username: @{u['username'] or '—'}\n"
        f"Телефон: {u['phone'] or '—'}\n"
        f"Email: {u.get('email') or '—'}\n"
        f"Статус: {u['status']}\n"
        f"Роли: {roles_str}"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "\u270f\ufe0f Имя", callback_data=f"admin_user_edit_{user_id}_name"
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f4f1 Телефон", callback_data=f"admin_user_edit_{user_id}_phone"
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f4e7 Email", callback_data=f"admin_user_edit_{user_id}_email"
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f3ab Роли", callback_data=f"admin_user_roles_{user_id}"
            )
        ],
    ]
    if u["status"] != "inactive":
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f6ab Блок", callback_data=f"admin_user_block_{user_id}"
                )
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\u2705 Разблокировать",
                    callback_data=f"admin_user_unblock_{user_id}",
                )
            ]
        )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_clients_page_1_all")]
    )
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await _safe_edit(update.callback_query, text, reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


# ----------------------------------------------------------------------
# Редактирование полей пользователя (имя/телефон/email)
# ----------------------------------------------------------------------
async def admin_user_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрос нового значения поля (имя/телефон/email) для пользователя."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END
    query = update.callback_query

    # callback: admin_user_edit_{user_id}_{field}
    try:
        rest = query.data[len("admin_user_edit_"):]
        user_id_str, field = rest.split("_")
        user_id = int(user_id_str)
    except (ValueError, AttributeError):
        await query.edit_message_text("Ошибка: неверный ID пользователя.")
        return ConversationHandler.END

    if field not in ADMIN_USER_FIELD_LABELS:
        await query.edit_message_text("Ошибка: неизвестное поле.")
        return ConversationHandler.END

    target = User.get_by_id(user_id)
    if not target:
        await safe_answer(query, "Пользователь не найден", show_alert=True)
        return ConversationHandler.END

    label = ADMIN_USER_FIELD_LABELS[field]
    db_field = "full_name" if field == "name" else field
    current = target.get(db_field) or "—"

    context.user_data["admin_user_edit_user_id"] = user_id
    context.user_data["admin_user_edit_field"] = field

    text = (
        f"\u270f\ufe0f Изменение «{label}»\n\n"
        f"Пользователь: {target.get('full_name') or target.get('username') or user_id}\n"
        f"Текущее значение: {current}\n\n"
        f"Введите новое значение для «{label}»:"
    )
    keyboard = [[InlineKeyboardButton("\U0001f519 Отмена", callback_data="admin_user_cancel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_USER_EDIT


async def admin_user_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Валидация и сохранение нового значения поля."""
    value = update.message.text.strip()
    user_id = context.user_data.get("admin_user_edit_user_id")
    field = context.user_data.get("admin_user_edit_field")

    if not user_id or field not in ADMIN_USER_FIELD_LABELS:
        await update.message.reply_text("Диалог устарел. Откройте карточку заново.")
        return ConversationHandler.END

    label = ADMIN_USER_FIELD_LABELS[field]
    error_text = None
    if field == "name" and len(value) < 3:
        error_text = "\u26a0\ufe0f Имя должно быть не короче 3 символов. Введите ещё раз:"
    elif field == "phone" and len(re.sub(r"\D", "", value)) < 7:
        error_text = "\u26a0\ufe0f Телефон должен содержать не менее 7 цифр. Введите ещё раз:"
    elif field == "email" and not re.match(r"^[^@\s]+@[^@\s]+$", value):
        error_text = "\u26a0\ufe0f Неверный email. Введите ещё раз:"

    if error_text:
        await update.message.reply_text(error_text)
        return ADMIN_USER_EDIT

    db_field = "full_name" if field == "name" else field
    User.update_field(user_id, db_field, value)
    logger.info(
        "Админ (telegram %s) изменил поле %s пользователя %s на %s",
        update.effective_user.id,
        db_field,
        user_id,
        value,
    )
    context.user_data.pop("admin_user_edit_user_id", None)
    context.user_data.pop("admin_user_edit_field", None)

    await update.message.reply_text(f"\u2705 {label} обновлено.")
    await show_admin_user_manage(update, context, user_id)
    return ConversationHandler.END


async def admin_user_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена редактирования поля (кнопка или /cancel)."""
    user_id = context.user_data.get("admin_user_edit_user_id")
    context.user_data.pop("admin_user_edit_user_id", None)
    context.user_data.pop("admin_user_edit_field", None)

    if update.callback_query:
        await safe_answer(update.callback_query)
        await update.callback_query.edit_message_text(
            "\u270f\ufe0f Редактирование отменено."
        )
    else:
        await update.message.reply_text("\u270f\ufe0f Редактирование отменено.")

    if user_id:
        await show_admin_user_manage(update, context, user_id)
    return ConversationHandler.END


# ----------------------------------------------------------------------
# Блокировка / разблокировка пользователя
# ----------------------------------------------------------------------
async def handle_admin_user_block(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
):
    """Блокировка пользователя (статус inactive)."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    u = User.get_by_id(user_id)
    if not u:
        await safe_answer(query, "Пользователь не найден", show_alert=True)
        return

    User.set_status(user_id, "inactive")
    logger.info("Админ %s заблокировал пользователя %s", admin["id"], user_id)
    await safe_answer(query, "\U0001f6ab Пользователь заблокирован", show_alert=True)
    await show_admin_user_manage(update, context, user_id)


async def handle_admin_user_unblock(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
):
    """Разблокировка пользователя (статус active)."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    u = User.get_by_id(user_id)
    if not u:
        await safe_answer(query, "Пользователь не найден", show_alert=True)
        return

    User.set_status(user_id, "active")
    logger.info("Админ %s разблокировал пользователя %s", admin["id"], user_id)
    await safe_answer(query, "\u2705 Пользователь разблокирован", show_alert=True)
    await show_admin_user_manage(update, context, user_id)


# ----------------------------------------------------------------------
# Управление ролями
# ----------------------------------------------------------------------
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
# Добавление пользователя (ConversationHandler)
# ----------------------------------------------------------------------
async def admin_user_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1 — добавление пользователя: запрос ФИО."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END
    query = update.callback_query

    context.user_data["new_user"] = {}
    await query.edit_message_text(
        "\u2795 Добавление пользователя\n\nВведите ФИО (например: Иванов Иван):"
    )
    return ADMIN_USER_ADD_NAME


async def admin_user_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2 — ФИО, запрос телефона."""
    name = update.message.text.strip()
    if len(name) < 3:
        await update.message.reply_text(
            "\u26a0\ufe0f ФИО должно содержать минимум 3 символа. Введите ещё раз:"
        )
        return ADMIN_USER_ADD_NAME

    context.user_data.setdefault("new_user", {})["full_name"] = name
    await update.message.reply_text(
        "Введите телефон (например: +375291234567):"
    )
    return ADMIN_USER_ADD_PHONE


async def admin_user_add_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3 — телефон, запрос Telegram ID."""
    phone = update.message.text.strip()
    if len(re.sub(r"\D", "", phone)) < 7:
        await update.message.reply_text(
            "\u26a0\ufe0f В телефоне должно быть минимум 7 цифр. Введите ещё раз:"
        )
        return ADMIN_USER_ADD_PHONE

    context.user_data.setdefault("new_user", {})["phone"] = phone
    await update.message.reply_text(
        "Введите Telegram ID (цифрами) или «-», если ID нет:"
    )
    return ADMIN_USER_ADD_TG


async def admin_user_add_tg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 4 — Telegram ID (может отсутствовать), запрос email."""
    raw = update.message.text.strip()
    tg_id = None
    if raw and raw != "-":
        try:
            tg_id = int(raw)
        except ValueError:
            await update.message.reply_text(
                "\u26a0\ufe0f Telegram ID должен быть числом или «-». Введите ещё раз:"
            )
            return ADMIN_USER_ADD_TG

    context.user_data.setdefault("new_user", {})["telegram_id"] = tg_id
    await update.message.reply_text(
        "Введите email или «-», если email нет:"
    )
    return ADMIN_USER_ADD_EMAIL


async def admin_user_add_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 5 — email, создание пользователя, выдача роли student."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END

    raw = update.message.text.strip()
    email = None
    if raw and raw != "-":
        if not re.match(r"^[^@\s]+@[^@\s]+$", raw):
            await update.message.reply_text(
                "\u26a0\ufe0f Неверный email. Введите ещё раз (или «-»):"
            )
            return ADMIN_USER_ADD_EMAIL
        email = raw

    data = context.user_data.get("new_user") or {}
    full_name = data.get("full_name") or ""
    phone = data.get("phone") or ""
    telegram_id = data.get("telegram_id")

    now = datetime.now()
    try:
        user_id = User.create(
            telegram_id=telegram_id,
            full_name=full_name,
            phone=phone,
            email=email,
            role="student",
            status="active",
            registered_at=now,
            consent_given=1,
            consent_date=now,
            consent_text_version=CONSENT_TEXT_VERSION,
        )
        UserRole.grant(user_id, "student", granted_by=admin["id"])
        logger.info(
            "Админ %s добавил пользователя %s (ID %s)", admin["id"], full_name, user_id
        )
        await update.message.reply_text(
            f"\u2705 Пользователь «{full_name}» создан."
        )
    except Exception as e:
        logger.error("Ошибка при создании пользователя: %s", e)
        await update.message.reply_text(
            "\u26a0\ufe0f Не удалось создать пользователя (возможно, такой Telegram ID уже есть)."
        )
        context.user_data.pop("new_user", None)
        return ConversationHandler.END

    context.user_data.pop("new_user", None)
    await show_admin_user_manage(update, context, user_id)
    return ConversationHandler.END


async def admin_user_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена добавления пользователя."""
    context.user_data.pop("new_user", None)
    if update.callback_query:
        await safe_answer(update.callback_query)
        await update.callback_query.edit_message_text(
            "\u274c Добавление пользователя отменено."
        )
    else:
        await update.message.reply_text("\u274c Добавление пользователя отменено.")
    await show_admin_clients(update, context)
    return ConversationHandler.END


# ----------------------------------------------------------------------
# Удаление заблокированных пользователей
# ----------------------------------------------------------------------
async def show_admin_user_delete_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Список заблокированных (status='inactive') пользователей для удаления."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, full_name, phone, telegram_id FROM users WHERE status = 'inactive' ORDER BY full_name"
            )
            users = cur.fetchall()

    if not users:
        text = "\U0001f5d1\ufe0f Заблокированные\n\nНет заблокированных пользователей."
        keyboard = [
            [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_clients_page_1_all")]
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))
        return

    lines = [f"\U0001f5d1\ufe0f Заблокированные (Всего: {len(users)})", ""]
    keyboard = []
    for u in users:
        name = u[1] or f"ID {u[3]}"
        lines.append(f"• {name} ({u[2] or '—'})")
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{name} (ID {u[0]})",
                    callback_data=f"admin_user_delete_confirm_{u[0]}",
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_clients_page_1_all")]
    )
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard))


async def admin_user_delete_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
):
    """Подтверждение полного удаления пользователя."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    u = User.get_by_id(user_id)
    if not u:
        await safe_answer(query, "Пользователь не найден", show_alert=True)
        return

    name = u.get("full_name") or u.get("username") or f"ID {u.get('telegram_id')}"
    text = (
        f"\u26a0\ufe0f Удалить пользователя «{name}»?\n\n"
        "Будут удалены все посещения, записи, абонементы, заморозки и роли. "
        "Действие необратимо."
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "\u2705 Да, удалить", callback_data=f"admin_user_delete_do_{user_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Нет", callback_data="admin_user_delete_list"
            )
        ],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def admin_user_delete_do(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
):
    """Полное удаление пользователя в транзакции."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("START TRANSACTION")
            # Прямые зависимости по user_id
            cur.execute("DELETE FROM visits WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM bookings WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))

            # Абонементы и их заморозки
            cur.execute(
                "SELECT id FROM subscriptions WHERE user_id = %s", (user_id,)
            )
            sub_ids = [r[0] for r in cur.fetchall()]
            if sub_ids:
                placeholders = ",".join(["%s"] * len(sub_ids))
                cur.execute(
                    f"DELETE FROM freezes WHERE subscription_id IN ({placeholders})",
                    sub_ids,
                )
                cur.execute(
                    f"DELETE FROM subscriptions WHERE id IN ({placeholders})",
                    sub_ids,
                )

            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            conn.commit()

        logger.info("Админ %s полностью удалил пользователя %s", admin["id"], user_id)
        await safe_answer(
            query, "\u2705 Пользователь полностью удалён", show_alert=True
        )
    except Exception as e:
        conn.rollback()
        logger.error("Ошибка при удалении пользователя %s: %s", user_id, e)
        await safe_answer(query, "\u26a0\ufe0f Не удалось удалить пользователя", show_alert=True)
    finally:
        conn.close()

    await show_admin_user_delete_list(update, context)