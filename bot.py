"""
Telegram-бот для студии танцев и йоги Soul.
Точка входа — только импорты, настройка Application и регистрация хендлеров.
"""
import asyncio
import logging
import os
import warnings

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Подавляем предупреждение PTBUserWarning от ConversationHandler
warnings.filterwarnings("ignore", message=".*If 'per_message=False'.*", category=UserWarning)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация при старте
from rag_agent import sync_knowledge
sync_knowledge()

# Импорт констант состояний из хендлеров
from handlers.student import (
    CONSENT,
    FULL_NAME,
    PHONE,
    PROFILE_FULL_NAME,
    PROFILE_PHONE,
    book_consent,
    book_full_name,
    book_phone,
    book_start,
    button_handler,
    cancel,
    profile_edit_full_name,
    profile_edit_phone,
    profile_edit_start,
    profile_cancel,
    start,
    ask_handler,
    text_handler,
    sync_events_cache,
)
from handlers.admin import (
    ADMIN_FREEZE_START,
    ADMIN_FREEZE_END,
    ADMIN_PROFILE_FIELD,
    ADMIN_PROFILE_VALUE,
    ADMIN_SUB_EXPIRES_DATE,
    admin_profile_cancel,
    admin_profile_choose_field,
    admin_profile_start,
    admin_profile_value,
    admin_sub_expires_cancel,
    admin_sub_expires_date,
    admin_sub_expires_handler,
    freeze_custom_cancel,
    freeze_custom_end_date,
    freeze_custom_start,
    freeze_custom_start_date,
)
from handlers.admin_users import (
    ADMIN_USER_ADD_EMAIL,
    ADMIN_USER_ADD_NAME,
    ADMIN_USER_ADD_PHONE,
    ADMIN_USER_ADD_TG,
    ADMIN_USER_EDIT,
    admin_user_add_cancel,
    admin_user_add_email,
    admin_user_add_name,
    admin_user_add_phone,
    admin_user_add_start,
    admin_user_add_tg,
    admin_user_edit_cancel,
    admin_user_edit_start,
    admin_user_edit_value,
)
from handlers.helpers import check_and_complete_freeze


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler для регистрации
    book_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(book_start, pattern="^book$")],
        states={
            CONSENT: [CallbackQueryHandler(book_consent, pattern="^consent_agree$")],
            FULL_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, book_full_name)
            ],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_phone)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # ConversationHandler для редактирования профиля
    profile_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(profile_edit_start, pattern="^profile_edit$")],
        states={
            PROFILE_FULL_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, profile_edit_full_name),
                CallbackQueryHandler(profile_cancel, pattern="^profile_cancel$"),
            ],
            PROFILE_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, profile_edit_phone),
                CallbackQueryHandler(profile_cancel, pattern="^profile_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", profile_cancel)],
    )

    # ConversationHandler смены даты окончания абонемента (админка)
    admin_sub_expires_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_sub_expires_handler, pattern=r"^admin_sub_expires_\d+$")
        ],
        states={
            ADMIN_SUB_EXPIRES_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_sub_expires_date),
                CallbackQueryHandler(admin_sub_expires_cancel, pattern=r"^admin_sub_expires_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_sub_expires_cancel)],
    )

    # ConversationHandler кастомной заморозки (даты), единый для админа и клиента
    freeze_custom_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(freeze_custom_start, pattern=r"^freeze_custom$"),
            CallbackQueryHandler(freeze_custom_start, pattern=r"^freeze_custom_\d+$"),
        ],
        states={
            ADMIN_FREEZE_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, freeze_custom_start_date)
            ],
            ADMIN_FREEZE_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, freeze_custom_end_date)
            ],
        },
        fallbacks=[CommandHandler("cancel", freeze_custom_cancel)],
    )

    # ConversationHandler редактирования профиля (супер-админ)
    admin_profile_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_profile_start, pattern=r"^admin_profile_edit_\d+$")
        ],
        states={
            ADMIN_PROFILE_FIELD: [
                CallbackQueryHandler(admin_profile_choose_field, pattern=r"^admin_profile_field_"),
                CallbackQueryHandler(admin_profile_cancel, pattern=r"^admin_profile_cancel$"),
            ],
            ADMIN_PROFILE_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_profile_value),
                CallbackQueryHandler(admin_profile_cancel, pattern=r"^admin_profile_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_profile_cancel)],
    )

    # ConversationHandler редактирования полей пользователя (админка)
    admin_user_edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_user_edit_start, pattern=r"^admin_user_edit_\d+_(name|phone|email)$")
        ],
        states={
            ADMIN_USER_EDIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_edit_value),
                CallbackQueryHandler(admin_user_edit_cancel, pattern=r"^admin_user_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_user_edit_cancel)],
    )

    # ConversationHandler добавления пользователя (админка)
    admin_user_add_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_user_add_start, pattern=r"^admin_user_add$")
        ],
        states={
            ADMIN_USER_ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_add_name)
            ],
            ADMIN_USER_ADD_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_add_phone)
            ],
            ADMIN_USER_ADD_TG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_add_tg)
            ],
            ADMIN_USER_ADD_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_add_email)
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_user_add_cancel)],
    )

    # -----------------------------------------------------------------------
    # Error handler
    # -----------------------------------------------------------------------
    async def error_handler(update: object, context):
        """Логирует ошибки и сообщает пользователю о сбое."""
        logger.error("Необработанная ошибка:", exc_info=context.error)
        if update and hasattr(update, "effective_message") and update.effective_message:
            await update.effective_message.reply_text(
                "\u26a0\ufe0f Что-то пошло не так. Попробуйте ещё раз или напишите /start."
            )

    application.add_error_handler(error_handler)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ask", ask_handler))
    application.add_handler(book_conv)
    application.add_handler(profile_conv)
    application.add_handler(admin_sub_expires_conv)
    application.add_handler(freeze_custom_conv)
    application.add_handler(admin_profile_conv)
    application.add_handler(admin_user_edit_conv)
    application.add_handler(admin_user_add_conv)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    application.add_handler(CallbackQueryHandler(button_handler))

    # ВАЖНО ДЛЯ ДЕПЛОЯ: Перед запуском на VPS убедитесь, что выполнены все миграции!
    # Команда: python3 db/migrations/run_migration.py 001_add_user_roles.sql
    # (и так далее по порядку — 002, 003... последняя)

    # Безопасная инициализация цикла событий (Python 3.12+)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    # Синхронизация кэша событий при старте
    try:
        loop.run_until_complete(sync_events_cache())
    except Exception as e:
        logger.warning("Не удалось синхронизировать кэш событий: %s", e)

    # Запуск в режиме polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
