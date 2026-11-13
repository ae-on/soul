"""
Хендлеры управления тарифами (типами абонементов).
"""
import logging
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

from db.models import AuditLog, SubscriptionType
from handlers.helpers import (
    _require_admin,
    _safe_edit,
    safe_answer,
)

logger = logging.getLogger(__name__)

# Состояния диалогов
EDIT_PRICE, EDIT_HOURS = range(40, 42)
ADD_NAME, ADD_HOURS, ADD_DAYS, ADD_PRICE = range(42, 46)


# ----------------------------------------------------------------------
# Список тарифов
# ----------------------------------------------------------------------
async def show_admin_types(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список всех тарифов (типов абонементов)."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    types = SubscriptionType.get_all()  # все типы, включая неактивные

    if not types:
        text = "\U0001f3ab Тарифы\n\nНет тарифов."
        keyboard = [
            [InlineKeyboardButton("\u2795 Добавить тариф", callback_data="admin_type_add")],
            [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")],
        ]
        await _safe_edit(query, text, InlineKeyboardMarkup(keyboard))
        return

    lines = ["\U0001f3ab Тарифы", ""]
    keyboard = []
    for t in types:
        name = t.get("name") or "—"
        price = t.get("price_byn") or 0
        hours = t.get("hours_count")
        hours_str = f"{hours}ч" if hours is not None else "безлимит"
        active = t.get("active")
        status = "✅" if active else "\u274c"
        lines.append(f"{status} {name} ({price} BYN, {hours_str})")
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{name} ({price} BYN)",
                    callback_data=f"admin_type_manage_{t['id']}",
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("\u2795 Добавить тариф", callback_data="admin_type_add")]
    )
    keyboard.append(
        [InlineKeyboardButton("\U0001f519 В админ-панель", callback_data="admin_panel")]
    )
    await _safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard))


# ----------------------------------------------------------------------
# Управление конкретным тарифом
# ----------------------------------------------------------------------
async def show_admin_type_manage(
    update: Update, context: ContextTypes.DEFAULT_TYPE, type_id: int
):
    """Карточка тарифа с кнопками редактирования."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    t = SubscriptionType.get_by_id(type_id)
    if not t:
        await safe_answer(query, "Тариф не найден", show_alert=True)
        return

    hours = t.get("hours_count")
    hours_str = f"{hours}ч" if hours is not None else "безлимит"
    status_text = "\u2705 Активен" if t.get("active") else "\u274c В архиве"

    text = (
        f"\U0001f3ab Тариф: {t.get('name') or '—'}\n"
        f"\u23f1 Часы: {hours_str}\n"
        f"\U0001f4c5 Срок: {t.get('duration_days')} дней\n"
        f"\U0001f4b0 Цена: {t.get('price_byn')} BYN\n"
        f"Статус: {status_text}"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4b0 Изменить цену", callback_data=f"admin_type_edit_price_{type_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "\u270f\ufe0f Изменить часы", callback_data=f"admin_type_edit_hours_{type_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f5d1\ufe0f Архивировать" if t.get("active") else "\U0001f504 Восстановить",
                callback_data=f"admin_type_toggle_{type_id}",
            )
        ],
        [InlineKeyboardButton("\U0001f519 Назад", callback_data="admin_types")],
    ]
    await _safe_edit(update, text, InlineKeyboardMarkup(keyboard))


# ----------------------------------------------------------------------
# Архивирование / восстановление
# ----------------------------------------------------------------------
async def handle_admin_type_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE, type_id: int
):
    """Переключение active (1 ↔ 0)."""
    admin = await _require_admin(update, context)
    if not admin:
        return
    query = update.callback_query

    t = SubscriptionType.get_by_id(type_id)
    if not t:
        await safe_answer(query, "Тариф не найден", show_alert=True)
        return

    new_active = 0 if t.get("active") else 1
    SubscriptionType.update(type_id, active=new_active)

    AuditLog.create(
        user_id=admin["id"],
        action="edit_type",
        table_name="subscription_types",
        record_id=type_id,
        new_value=f'{{"active": {new_active}}}',
    )
    logger.info(
        "Админ %s изменил active=%s у тарифа %s", admin["id"], new_active, type_id
    )
    await safe_answer(
        query,
        "\u2705 Тариф архивирован" if new_active == 0 else "\u2705 Тариф восстановлен",
        show_alert=True,
    )
    await show_admin_type_manage(update, context, type_id)


# ----------------------------------------------------------------------
# Диалог изменения цены
# ----------------------------------------------------------------------
async def admin_type_edit_price_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Запрос новой цены."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END
    query = update.callback_query

    try:
        type_id = int(query.data.replace("admin_type_edit_price_", ""))
    except (ValueError, AttributeError):
        await query.edit_message_text("Ошибка: неверный ID.")
        return ConversationHandler.END

    t = SubscriptionType.get_by_id(type_id)
    if not t:
        await safe_answer(query, "Тариф не найден", show_alert=True)
        return ConversationHandler.END

    context.user_data["edit_type_id"] = type_id
    await query.edit_message_text(
        f"\U0001f4b0 Текущая цена: {t['price_byn']} BYN\n\nВведите новую цену (число):"
    )
    return EDIT_PRICE


async def admin_type_edit_price_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Сохранение цены."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END

    raw = update.message.text.strip()
    try:
        new_price = float(raw)
        if new_price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "\u26a0\ufe0f Введите положительное число (например: 145):"
        )
        return EDIT_PRICE

    type_id = context.user_data.get("edit_type_id")
    context.user_data.pop("edit_type_id", None)
    if not type_id:
        await update.message.reply_text("Диалог устарел. Начните заново.")
        return ConversationHandler.END

    SubscriptionType.update(type_id, price_byn=new_price)
    AuditLog.create(
        user_id=admin["id"],
        action="edit_type",
        table_name="subscription_types",
        record_id=type_id,
        new_value=f'{{"price_byn": {new_price}}}',
    )
    logger.info("Админ %s изменил цену тарифа %s на %s", admin["id"], type_id, new_price)
    await _safe_edit(update, f"\u2705 Цена обновлена: {new_price} BYN")
    await show_admin_type_manage(update, context, type_id)
    return ConversationHandler.END


# ----------------------------------------------------------------------
# Диалог изменения часов
# ----------------------------------------------------------------------
async def admin_type_edit_hours_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Запрос нового количества часов."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END
    query = update.callback_query

    try:
        type_id = int(query.data.replace("admin_type_edit_hours_", ""))
    except (ValueError, AttributeError):
        await query.edit_message_text("Ошибка: неверный ID.")
        return ConversationHandler.END

    t = SubscriptionType.get_by_id(type_id)
    if not t:
        await safe_answer(query, "Тариф не найден", show_alert=True)
        return ConversationHandler.END

    context.user_data["edit_type_id"] = type_id
    current = t.get("hours_count")
    current_str = f"{current}ч" if current is not None else "безлимит"
    await query.edit_message_text(
        f"\u23f1 Текущие часы: {current_str}\n\n"
        "Введите новое количество часов (число, например 8 или 1.5, или «-» для безлимита):"
    )
    return EDIT_HOURS


async def admin_type_edit_hours_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Сохранение часов."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END

    raw = update.message.text.strip()
    if raw == "-":
        new_hours = None
    else:
        try:
            new_hours = float(raw)
            if new_hours <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "\u26a0\ufe0f Введите положительное число (например: 8) или «-» для безлимита:"
            )
            return EDIT_HOURS

    type_id = context.user_data.get("edit_type_id")
    context.user_data.pop("edit_type_id", None)
    if not type_id:
        await update.message.reply_text("Диалог устарел. Начните заново.")
        return ConversationHandler.END

    SubscriptionType.update(type_id, hours_count=new_hours)
    AuditLog.create(
        user_id=admin["id"],
        action="edit_type",
        table_name="subscription_types",
        record_id=type_id,
        new_value=f'{{"hours_count": {new_hours}}}',
    )
    hours_str = f"{new_hours}ч" if new_hours is not None else "безлимит"
    logger.info("Админ %s изменил часы тарифа %s на %s", admin["id"], type_id, hours_str)
    await update.message.reply_text(f"\u2705 Часы обновлены: {hours_str}")
    await show_admin_type_manage(update, context, type_id)
    return ConversationHandler.END


# ----------------------------------------------------------------------
# Общий cancel для диалогов
# ----------------------------------------------------------------------
async def admin_type_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена редактирования тарифа."""
    context.user_data.pop("edit_type_id", None)
    if update.callback_query:
        await safe_answer(update.callback_query)
        await _safe_edit(update.callback_query, "\u270f\ufe0f Редактирование отменено.")
    else:
        await update.message.reply_text("\u270f\ufe0f Редактирование отменено.")
    return ConversationHandler.END


# ----------------------------------------------------------------------
# Добавление нового тарифа
# ----------------------------------------------------------------------
async def admin_type_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1 — запрос названия."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END
    query = update.callback_query

    context.user_data["new_type"] = {}
    await query.edit_message_text(
        "\u2795 Добавление тарифа\n\nВведите название (например: 10 занятий):"
    )
    return ADD_NAME


async def admin_type_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2 — название, запрос часов."""
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("\u26a0\ufe0f Название слишком короткое. Введите ещё раз:")
        return ADD_NAME

    context.user_data.setdefault("new_type", {})["name"] = name
    await update.message.reply_text(
        "Введите количество часов (число, например 8, или «-» для безлимита):"
    )
    return ADD_HOURS


async def admin_type_add_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3 — часы, запрос срока."""
    raw = update.message.text.strip()
    if raw == "-":
        hours = None
    else:
        try:
            hours = float(raw)
            if hours <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "\u26a0\ufe0f Введите положительное число или «-»:"
            )
            return ADD_HOURS

    context.user_data.setdefault("new_type", {})["hours_count"] = hours
    await update.message.reply_text(
        "Введите срок действия в днях (число, например 30):"
    )
    return ADD_DAYS


async def admin_type_add_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 4 — срок, запрос цены."""
    raw = update.message.text.strip()
    try:
        days = int(raw)
        if days <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "\u26a0\ufe0f Введите положительное целое число (например 30):"
        )
        return ADD_DAYS

    context.user_data.setdefault("new_type", {})["duration_days"] = days
    await update.message.reply_text(
        "Введите цену в BYN (число, например 145):"
    )
    return ADD_PRICE


async def admin_type_add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 5 — цена, создание тарифа."""
    admin = await _require_admin(update, context)
    if not admin:
        return ConversationHandler.END

    raw = update.message.text.strip()
    try:
        price = float(raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "\u26a0\ufe0f Введите положительное число (например 145):"
        )
        return ADD_PRICE

    data = context.user_data.get("new_type") or {}
    name = data.get("name", "Новый тариф")
    hours = data.get("hours_count")
    days = data.get("duration_days", 30)
    context.user_data.pop("new_type", None)

    try:
        new_id = SubscriptionType.create(
            name=name,
            hours_count=hours,
            duration_days=days,
            price_byn=price,
            active=1,
            sort_order=0,
        )
        AuditLog.create(
            user_id=admin["id"],
            action="edit_type",
            table_name="subscription_types",
            record_id=new_id,
            new_value=f'{{"created": true, "name": "{name}"}}',
        )
        logger.info("Админ %s создал тариф %s (ID %s)", admin["id"], name, new_id)
        await update.message.reply_text(f"\u2705 Тариф «{name}» создан!")
    except Exception as e:
        logger.error("Ошибка при создании тарифа: %s", e)
        await update.message.reply_text("\u26a0\ufe0f Не удалось создать тариф.")
        return ConversationHandler.END

    await show_admin_types(update, context)
    return ConversationHandler.END


async def admin_type_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена создания тарифа."""
    context.user_data.pop("new_type", None)
    if update.callback_query:
        await safe_answer(update.callback_query)
        await _safe_edit(update.callback_query, "\u274c Создание тарифа отменено.")
    else:
        await update.message.reply_text("\u274c Создание тарифа отменено.")
    await show_admin_types(update, context)
    return ConversationHandler.END