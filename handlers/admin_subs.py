"""
Хендлеры продажи абонемента клиенту (админка и панель преподавателя).
"""
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from db.models import SubscriptionType, User, UserRole
from handlers.admin import show_admin_user_subs
from handlers.helpers import (
    _extract_int,
    _extract_two_ints,
    _safe_edit,
    safe_answer,
)
from handlers.teacher import show_teacher_panel
from services.sales import process_sale

logger = logging.getLogger(__name__)

# Состояния диалога продажи
SELL_CLIENT, SELL_TYPE, SELL_DATE, SELL_PAYMENT = range(50, 54)

# Способы оплаты: ключ (значение из кнопок) -> подпись кнопки.
# В БД (payments.method) 'card' нормализуется в 'bepaid' внутри process_sale.
PAYMENT_METHODS = {
    "cash": "\U0001f4b5 Наличные",
    "card": "\U0001f4b3 Карта",
    "other": "\U0001f91d Другое",
}


async def _require_seller(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверяет, что пользователь — admin, super_admin или teacher."""
    user = User.get_by_telegram_id(update.effective_user.id)
    if not user:
        await safe_answer(update.callback_query, "Не зарегистрирован", show_alert=True)
        return None
    roles = UserRole.get_active_roles(user["id"])
    if not any(r in roles for r in ("admin", "super_admin", "teacher")):
        await safe_answer(update.callback_query, "Нет доступа", show_alert=True)
        return None
    return user


def _client_name(user: dict) -> str:
    """Имя клиента для подписей кнопок и текстов."""
    return user.get("full_name") or user.get("username") or f"ID {user['id']}"


async def _sell_show_types(update, context, client_id: int, name: str):
    """Общий шаг: список тарифов на выбор (используется админом и преподавателем)."""
    types = SubscriptionType.get_active()
    if not types:
        await _safe_edit(update, "Нет доступных тарифов.")
        return ConversationHandler.END

    context.user_data["sell_client_id"] = client_id
    text = f"\U0001f6d2 Продажа абонемента: {name}\n\nВыберите тариф:"
    keyboard = [
        [
            InlineKeyboardButton(
                f"{t['name']} ({t['price_byn']} BYN)",
                callback_data=f"admin_sell_type_{client_id}_{t['id']}",
            )
        ]
        for t in types
    ]
    keyboard.append(
        [InlineKeyboardButton("\U0001f6ab Отмена", callback_data="admin_sell_cancel")]
    )
    await _safe_edit(update, text, InlineKeyboardMarkup(keyboard))
    return SELL_TYPE


# ----------------------------------------------------------------------
# Вход в диалог
# ----------------------------------------------------------------------
async def admin_sell_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало продажи из админки: клиент известен (admin_sell_{user_id})."""
    seller = await _require_seller(update, context)
    if not seller:
        return ConversationHandler.END
    query = update.callback_query
    user_id = _extract_int(query.data, "admin_sell_")
    user = User.get_by_id(user_id)
    if not user:
        await safe_answer(query, "Клиент не найден", show_alert=True)
        return ConversationHandler.END
    context.user_data["sell_origin"] = "admin"
    return await _sell_show_types(update, context, user_id, _client_name(user))


async def teacher_sell_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало продажи из панели преподавателя: выбор клиента."""
    seller = await _require_seller(update, context)
    if not seller:
        return ConversationHandler.END
    context.user_data["sell_origin"] = "teacher"

    students = User.get_students_for_admin(limit=10)
    keyboard = [
        [
            InlineKeyboardButton(
                _client_name(u), callback_data=f"teacher_sell_client_{u['id']}"
            )
        ]
        for u in students
    ]
    keyboard.append(
        [
            InlineKeyboardButton(
                "\U0001f50d Поиск по ID или ФИО", callback_data="teacher_sell_search"
            )
        ]
    )
    keyboard.append(
        [InlineKeyboardButton("\U0001f6ab Отмена", callback_data="admin_sell_cancel")]
    )
    await _safe_edit(
        update,
        "\U0001f6d2 Продажа абонемента\n\nВыберите клиента из списка:",
        InlineKeyboardMarkup(keyboard),
    )
    return SELL_CLIENT


async def teacher_sell_client_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало продажи из карточки клиента преподавателя (клиент известен)."""
    seller = await _require_seller(update, context)
    if not seller:
        return ConversationHandler.END
    query = update.callback_query
    user_id = _extract_int(query.data, "teacher_sell_manage_")
    user = User.get_by_id(user_id)
    if not user:
        await safe_answer(query, "Клиент не найден", show_alert=True)
        return ConversationHandler.END
    context.user_data["sell_origin"] = "teacher"
    context.user_data["sell_client_id"] = user_id
    return await _sell_show_types(update, context, user_id, _client_name(user))


# ----------------------------------------------------------------------
# Шаг 0 (только преподаватель): выбор клиента
# ----------------------------------------------------------------------
async def teacher_sell_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрос Telegram ID или ФИО клиента."""
    seller = await _require_seller(update, context)
    if not seller:
        return ConversationHandler.END
    await _safe_edit(
        update,
        "\U0001f50d Введите Telegram ID клиента или ФИО\n"
        "(например: 123456789 или Иванова Анна):",
    )
    return SELL_CLIENT


async def teacher_sell_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор клиента из списка учеников."""
    seller = await _require_seller(update, context)
    if not seller:
        return ConversationHandler.END
    query = update.callback_query
    user_id = _extract_int(query.data, "teacher_sell_client_")
    user = User.get_by_id(user_id)
    if not user:
        await safe_answer(query, "Клиент не найден", show_alert=True)
        return SELL_CLIENT
    # Надёжно сохраняем клиента в user_data — при оплате берём ID отсюда
    context.user_data["sell_client_id"] = user_id
    return await _sell_show_types(update, context, user_id, _client_name(user))


async def teacher_sell_client_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск клиента по Telegram ID или ФИО (текстовый ввод)."""
    seller = await _require_seller(update, context)
    if not seller:
        return ConversationHandler.END
    raw = update.message.text.strip()

    # Поиск по Telegram ID
    if raw.isdigit():
        user = User.get_by_telegram_id(int(raw))
        if user:
            context.user_data["sell_client_id"] = user["id"]
            return await _sell_show_types(update, context, user["id"], _client_name(user))
        await update.message.reply_text(
            "Пользователь с таким Telegram ID не найден. Попробуйте ещё раз или /cancel."
        )
        return SELL_CLIENT

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
        u = matches[0]
        context.user_data["sell_client_id"] = u["id"]
        return await _sell_show_types(update, context, u["id"], _client_name(u))
    if len(matches) > 1:
        keyboard = [
            [
                InlineKeyboardButton(
                    _client_name(u), callback_data=f"teacher_sell_client_{u['id']}"
                )
            ]
            for u in matches[:10]
        ]
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f6ab Отмена", callback_data="admin_sell_cancel"
                )
            ]
        )
        await update.message.reply_text(
            "Найдено несколько клиентов. Уточните выбор:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return SELL_CLIENT

    await update.message.reply_text("Никого не найдено. Попробуйте ещё раз или /cancel.")
    return SELL_CLIENT


# ----------------------------------------------------------------------
# Шаг 1: выбор тарифа
# ----------------------------------------------------------------------
async def admin_sell_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбран тариф — запрашиваем дату старта."""
    seller = await _require_seller(update, context)
    if not seller:
        return ConversationHandler.END
    query = update.callback_query
    parsed = _extract_two_ints(query.data, "admin_sell_type_")
    if not parsed:
        await safe_answer(query, "Ошибка данных", show_alert=True)
        return ConversationHandler.END
    client_id, type_id = parsed
    sub_type = SubscriptionType.get_by_id(type_id)
    if not sub_type:
        await safe_answer(query, "Тариф не найден", show_alert=True)
        return ConversationHandler.END

    context.user_data["sell_client_id"] = client_id
    context.user_data["sell_type_id"] = type_id

    text = (
        f"\U0001f6d2 Тариф: {sub_type['name']} — {sub_type['price_byn']} BYN\n\n"
        "Когда начать действие абонемента?"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4c5 Сегодня", callback_data="admin_sell_date_today"
            )
        ],
        [InlineKeyboardButton("\U0001f6ab Отмена", callback_data="admin_sell_cancel")],
    ]
    await _safe_edit(update, text, InlineKeyboardMarkup(keyboard))
    return SELL_DATE


# ----------------------------------------------------------------------
# Шаг 2: дата старта
# ----------------------------------------------------------------------
async def admin_sell_date_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Дата старта — сегодня."""
    seller = await _require_seller(update, context)
    if not seller:
        return ConversationHandler.END
    context.user_data["sell_start_date"] = None
    return await _sell_show_payment(update, context)


async def admin_sell_date_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Дата старта — ввод текстом (ДД.ММ.ГГГГ)."""
    seller = await _require_seller(update, context)
    if not seller:
        return ConversationHandler.END
    raw = update.message.text.strip()
    try:
        start = datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        await update.message.reply_text(
            "\u26a0\ufe0f Неверный формат даты. Введите дату в формате ДД.ММ.ГГГГ "
            "(например, 25.12.2026) или нажмите «\U0001f4c5 Сегодня»."
        )
        return SELL_DATE
    context.user_data["sell_start_date"] = start.strftime("%d.%m.%Y")
    return await _sell_show_payment(update, context)


async def _sell_show_payment(update, context):
    """Шаг 3: выбор способа оплаты."""
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"admin_sell_pay_{key}")]
        for key, label in PAYMENT_METHODS.items()
    ]
    keyboard.append(
        [InlineKeyboardButton("\U0001f6ab Отмена", callback_data="admin_sell_cancel")]
    )
    await _safe_edit(update, "\U0001f4b3 Способ оплаты:", InlineKeyboardMarkup(keyboard))
    return SELL_PAYMENT


# ----------------------------------------------------------------------
# Шаг 4: оплата и оформление продажи
# ----------------------------------------------------------------------
async def admin_sell_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбран способ оплаты — оформляем продажу через process_sale."""
    seller = await _require_seller(update, context)
    if not seller:
        return ConversationHandler.END
    query = update.callback_query
    method = query.data[len("admin_sell_pay_"):]
    if method not in PAYMENT_METHODS:
        await safe_answer(query, "Неизвестный способ оплаты", show_alert=True)
        return SELL_PAYMENT

    client_id = context.user_data.get("sell_client_id")
    type_id = context.user_data.get("sell_type_id")
    start_date = context.user_data.get("sell_start_date")
    origin = context.user_data.get("sell_origin", "admin")
    if not client_id or not type_id:
        await safe_answer(query, "Диалог устарел. Начните заново.", show_alert=True)
        return ConversationHandler.END

    try:
        await process_sale(
            context.bot, client_id, type_id, method, seller["id"], start_date
        )
    except Exception as e:
        logger.error("Ошибка продажи абонемента: %s", e)
        await safe_answer(query, "Не удалось оформить продажу", show_alert=True)
        return ConversationHandler.END

    for key in ("sell_client_id", "sell_type_id", "sell_start_date", "sell_origin"):
        context.user_data.pop(key, None)

    client = User.get_by_id(client_id)
    client_name = _client_name(client) if client else client_id
    if origin == "teacher":
        await safe_answer(query, "\u2705 Продажа оформлена", show_alert=True)
        await show_teacher_panel(update, context)
    else:
        await show_admin_user_subs(
            update,
            context,
            client_id,
            notice=f"\u2705 Абонемент продан клиенту {client_name}",
        )
    return ConversationHandler.END


async def admin_sell_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена продажи (кнопка или /cancel)."""
    seller = await _require_seller(update, context)
    if not seller:
        return ConversationHandler.END
    for key in ("sell_client_id", "sell_type_id", "sell_start_date", "sell_origin"):
        context.user_data.pop(key, None)
    query = update.callback_query
    if query:
        await safe_answer(query, "Продажа отменена")
    await _safe_edit(update, "\U0001f6ab Продажа отменена.")
    return ConversationHandler.END
