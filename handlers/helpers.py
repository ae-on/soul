"""
Общие утилиты и константы для хендлеров.
"""
import json
import logging
import math
import re
from datetime import date, datetime, timedelta
from typing import Optional

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from db.connection import get_connection
from db.models import (
    Freeze,
    Subscription,
    SubscriptionType,
    User,
    UserRole,
    Visit,
)

logger = logging.getLogger(__name__)

# Константы
RULES_URL = "https://soul.by/ceni-na-tanci-minsk/"
FREEZE_MAX_DAYS = 14
CLIENTS_PER_PAGE = 10
SLOTS_PER_PAGE = 10

USER_STATUS_EMOJI = {
    "new": "\U0001f195",
    "active": "\u2705",
    "expired": "\u23f3",
    "archived": "\U0001f4e6",
    "inactive": "\u26aa",
    "deleted_data": "\U0001f5d1\ufe0f",
}

ADMIN_SUB_STATUS_LABELS = {
    "active": "\U0001f7e2 Активный",
    "frozen": "\U0001f9ca Заморожен",
    "expired": "\U0001f534 Сгорел",
    "depleted": "\U0001f534 Завершён",
    "cancelled": "\U0001f534 Отменён",
}

ADMIN_PROFILE_FIELDS = {
    "name": "ФИО",
    "phone": "Телефон",
    "email": "Email",
    "notes": "Заметки",
}


# ---------------------------------------------------------------------------
# Безопасные ответы и редактирование
# ---------------------------------------------------------------------------
async def safe_answer(query, text=None, show_alert=False):
    """Безопасно отвечает на callback, игнорируя временные сетевые ошибки."""
    try:
        await query.answer(text=text, show_alert=show_alert)
    except Exception as e:
        logger.warning("Не удалось ответить на callback (%s): %s", type(e).__name__, e)


async def _safe_edit(
    update: Update | CallbackQuery | None,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    """
    Редактирует сообщение. Принимает Update (новый формат: _safe_edit(update, ...))
    или CallbackQuery (старый формат: _safe_edit(query, ...)).
    Игнорирует «Message is not modified».
    """
    if isinstance(update, Update):
        query = update.callback_query
        if query:
            try:
                await query.edit_message_text(text, reply_markup=reply_markup)
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    pass
                else:
                    raise
        elif update.message:
            await update.message.reply_text(text, reply_markup=reply_markup)
        elif update.effective_chat:
            await update.effective_chat.send_message(text, reply_markup=reply_markup)
    elif isinstance(update, CallbackQuery):
        # Старый формат вызова: _safe_edit(query, ...) — CallbackQuery передаётся напрямую
        try:
            await update.edit_message_text(text, reply_markup=reply_markup)
        except BadRequest as e:
            if "Message is not modified" in str(e):
                pass
            else:
                raise


async def notify_admin(
    context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None
) -> bool:
    """Отправляет уведомление администратору (ADMIN_CHAT_ID из .env)."""
    import os
    from dotenv import load_dotenv
    load_dotenv()
    admin_chat_id = os.getenv("ADMIN_CHAT_ID")
    if not admin_chat_id:
        logger.info("ADMIN_CHAT_ID не задан — уведомление не отправлено")
        return False
    try:
        await context.bot.send_message(
            chat_id=admin_chat_id, text=text, reply_markup=reply_markup
        )
        return True
    except Exception as e:
        logger.error("Не удалось отправить уведомление админу: %s", e)
        return False


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------
def _format_date(value) -> str:
    """Форматирует дату как ДД.ММ.ГГГГ; None превращает в «—»."""
    if value is None:
        return "—"
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y")
    return str(value)[:10]


def format_client_card_info(user_id: int) -> str:
    """
    Строка с информацией об активном абонементе клиента для карточки.
    Пример:
      🎫 Абонемент: 8 часов (Осталось: 6.5ч, Действует до: 25.12.2026)
      🧊 Заморозка: активна с 01.12.2026 по 07.12.2026
    Если абонемента нет: «🎫 Абонемент: Нет активного.»
    """
    sub = Subscription.get_active_for_user(user_id)
    if not sub:
        return "\U0001f3ab Абонемент: Нет активного."

    sub_type = (
        SubscriptionType.get_by_id(sub["type_id"]) if sub.get("type_id") else None
    )
    type_name = sub_type["name"] if sub_type else "Абонемент"
    hours_left = sub.get("hours_left")
    hours_str = f"{hours_left}ч" if hours_left is not None else "безлимит"
    expires_str = _format_date(sub.get("expires_at"))
    lines = [
        f"\U0001f3ab Абонемент: {type_name} "
        f"(Осталось: {hours_str}, Действует до: {expires_str})"
    ]

    freeze = Freeze.get_active_for_subscription(sub["id"])
    if freeze:
        lines.append(
            f"\U0001f9ca Заморозка: активна с {_format_date(freeze['start_date'])} "
            f"по {_format_date(freeze['end_date'])}"
        )
    return "\n".join(lines)


def _format_time(value) -> str:
    """Форматирует время как ЧЧ:ММ (из timedelta/time/строки); None — «—»."""
    if value is None:
        return "—"
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    parts = str(value).split(":")
    if len(parts) >= 2:
        return f"{int(parts[0]):02d}:{parts[1]}"
    return str(value)


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
        suffix = f"{float(hours_left):g}ч"
    elif u.get("status") == "new":
        suffix = "новый"
    else:
        suffix = "нет абона"
    return f"{name} · {suffix}"


# ---------------------------------------------------------------------------
# Парсинг callback_data
# ---------------------------------------------------------------------------
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


def _parse_freeze_date(raw: str) -> Optional[date]:
    """Парсит дату ДД.ММ.ГГГГ; None при неверном формате."""
    try:
        return datetime.strptime(raw, "%d.%m.%Y").date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Проверки ролей
# ---------------------------------------------------------------------------
async def _require_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[dict]:
    """Проверяет, что пользователь — admin или super_admin."""
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
    """Проверяет, что пользователь — teacher (или admin/super_admin)."""
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


# ---------------------------------------------------------------------------
# Заморозка — общие утилиты
# ---------------------------------------------------------------------------
def _get_user_and_active_sub(update: Update) -> tuple[Optional[dict], Optional[dict]]:
    """Возвращает (user, активный абонемент) или (None, None)."""
    user = User.get_by_telegram_id(update.effective_user.id)
    if not user:
        return None, None
    return user, Subscription.get_active_for_user(user["id"])


def _calc_freeze_period(expires_at) -> tuple[date, date, int]:
    """
    Рассчитывает период заморозки: start=сегодня, end=min(сегодня+14, срок), days.
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


def check_and_complete_freeze(user_id: int) -> bool:
    """
    Автозавершение активной заморозки при возврате клиента.
    """
    sub = Subscription.get_active_for_user(user_id)
    if not sub:
        return False
    freeze = Freeze.get_approved_started(sub["id"])
    if not freeze:
        return False
    Freeze.complete_and_revert(freeze["id"], date.today())
    return True
