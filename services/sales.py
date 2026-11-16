"""
Сервис продажи абонемента: подписка + платёж + аудит-лог + уведомления.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv

from db.models import AuditLog, Payment, Subscription, SubscriptionType, User

logger = logging.getLogger(__name__)

# Человекочитаемые названия способов оплаты (ключ = значение из кнопок выбора)
PAYMENT_METHOD_LABELS = {
    "cash": "наличные",
    "card": "карта",
    "other": "другое",
    # устаревшие значения — оставлены для отображения старых записей
    "bepaid": "карта",
    "erip": "ЕРИП",
    "webpay": "карта",
}

# Приведение способа оплаты к значениям ENUM payments.method
# ('cash','erip','bepaid','webpay','other'): 'card' хранится в БД как 'bepaid'
DB_PAYMENT_METHODS = {
    "cash": "cash",
    "card": "bepaid",
    "other": "other",
}


async def process_sale(
    bot,
    client_id: int,
    type_id: int,
    payment_method: str,
    sold_by_id: int,
    start_date: Optional[str] = None,
):
    """
    Оформляет продажу абонемента клиенту.

    - Создаёт Subscription (status='active', expires_at = start_date + duration_days).
    - Создаёт Payment (amount = цена тарифа, status='paid').
    - Пишет AuditLog (кто продал, кому, какой тариф).
    - Уведомляет клиента и админов (ADMIN_CHAT_ID).
    - Возвращает созданный абонемент.
    """
    try:
        # 1. Тариф
        sub_type = SubscriptionType.get_by_id(type_id)
        if not sub_type:
            raise ValueError(f"Тариф с id={type_id} не найден")

        # 2. Дата старта (по умолчанию — сегодня)
        if start_date:
            start = datetime.strptime(start_date, "%d.%m.%Y")
        else:
            start = datetime.now()

        # 3. Абонемент
        hours = sub_type.get("hours_count")
        if hours is not None:
            hours = float(hours)
        expires_at = start + timedelta(days=sub_type.get("duration_days") or 30)
        sub_id = Subscription.create(
            user_id=client_id,
            type_id=type_id,
            purchased_at=datetime.now(),
            started_at=start,
            expires_at=expires_at,
            hours_total=hours,
            hours_left=hours,
            status="active",
        )

        # 4. Платёж
        db_method = DB_PAYMENT_METHODS.get(payment_method, payment_method)
        Payment.create(
            user_id=client_id,
            amount=sub_type["price_byn"],
            currency="BYN",
            method=db_method,
            status="paid",
            subscription_type_id=type_id,
            subscription_id=sub_id,
            description=f"Продажа абонемента {sub_type['name']}",
        )

        # 5. Аудит
        AuditLog.create(
            user_id=sold_by_id,
            action="sell_subscription",
            table_name="subscriptions",
            record_id=sub_id,
            new_value=json.dumps(
                {
                    "client_id": client_id,
                    "type_id": type_id,
                    "amount": float(sub_type["price_byn"]),
                    "method": payment_method,
                },
                ensure_ascii=False,
            ),
        )

        # 6. Уведомление клиенту
        client = User.get_by_id(client_id)
        if client and client.get("telegram_id"):
            try:
                await bot.send_message(
                    chat_id=client["telegram_id"],
                    text=(
                        f"Вам оформлен абонемент {sub_type['name']}. "
                        f"Действует до {expires_at.strftime('%d.%m.%Y')}."
                    ),
                )
            except Exception as e:
                logger.warning("Не удалось уведомить клиента %s: %s", client_id, e)

        # 7. Уведомление админам (ADMIN_CHAT_ID)
        load_dotenv()
        admin_chat_id = os.getenv("ADMIN_CHAT_ID")
        if admin_chat_id:
            seller = User.get_by_id(sold_by_id)
            seller_name = (
                (seller.get("full_name") or seller.get("username") or sold_by_id)
                if seller
                else sold_by_id
            )
            client_name = (
                (client.get("full_name") or client.get("username") or client_id)
                if client
                else client_id
            )
            method_label = PAYMENT_METHOD_LABELS.get(payment_method, payment_method)
            try:
                await bot.send_message(
                    chat_id=admin_chat_id,
                    text=(
                        f"Пользователь {seller_name} продал абонемент {sub_type['name']} "
                        f"клиенту {client_name} за {sub_type['price_byn']} BYN "
                        f"({method_label})."
                    ),
                )
            except Exception as e:
                logger.error("Не удалось уведомить админов о продаже: %s", e)

        # 8. Возвращаем созданный абонемент
        return Subscription.get_by_id(sub_id)
    except Exception:
        logger.error(
            "Ошибка продажи абонемента: client_id=%s, type_id=%s, "
            "payment_method=%s, sold_by_id=%s, start_date=%s",
            client_id,
            type_id,
            payment_method,
            sold_by_id,
            start_date,
            exc_info=True,
        )
        raise
