"""
HTTP-сервер для приёма вебхуков от системы ЕРИП (WebPay).

Работает в фоне параллельно с Telegram-ботом (systemd / nohup).
Запуск: uvicorn webhook_server:app --host 0.0.0.0 --port 8000
"""
import logging

from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Soul Studio ERIP Webhook")


@app.post("/erip/webhook")
async def erip_webhook(request: Request):
    """
    Принимает уведомления об оплате от ЕРИП (WebPay/bePaid).

    WebPay может слать данные как form-urlencoded (как в старом PHP),
    так и JSON — обрабатываем оба варианта.
    """
    data = {}
    content_type = request.headers.get("content-type", "").lower()

    try:
        if "application/json" in content_type:
            data = await request.json()
        else:
            # form-urlencoded (или любой другой тип) — читаем как форму
            data = dict(await request.form())
    except Exception:
        # Страховка: контент-тип может не совпадать с реальным форматом
        try:
            data = await request.json()
        except Exception:
            try:
                data = dict(await request.form())
            except Exception as e:
                logger.error("Ошибка при чтении вебхука от ЕРИП: %s", e)
                data = {}

    logger.info(f"Получен вебхук от ЕРИП: {data}")

    # TODO: обработка платежа (проверка подписи, начисление часов, уведомление клиента)
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
