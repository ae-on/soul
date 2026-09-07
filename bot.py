import logging
import os

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import init_db, save_user, update_user_details

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

logging.basicConfig(level=logging.INFO)

# Состояния для диалога записи
FULL_NAME, PHONE = range(2)

# Инициализируем БД при старте
init_db()


# ----------------------------------------------------------------------
# Вспомогательная функция — показать главное меню
# ----------------------------------------------------------------------
async def show_main_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None
):
    """Отправляет (или редактирует) главное меню."""
    keyboard = [
        [InlineKeyboardButton("📅 Расписание и запись", callback_data="schedule")],
        [InlineKeyboardButton("💰 Абонементы и цены", callback_data="prices")],
        [InlineKeyboardButton("📍 О студии", callback_data="info")],
        [InlineKeyboardButton("🧭 Направления", callback_data="directions")],
        [InlineKeyboardButton("💬 Задать вопрос", callback_data="support")],
        [InlineKeyboardButton("✍️ Записаться на занятие", callback_data="book")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if text is None:
        text = "Выберите действие в меню ниже:"

    # Если это callback — редактируем сообщение, иначе — отправляем новое
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


# ----------------------------------------------------------------------
# Подменю "Направления"
# ----------------------------------------------------------------------
async def show_directions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает подменю с направлениями."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("💃 Бачата", callback_data="bachata_info")],
        [InlineKeyboardButton("🔥 Трайбл", callback_data="tribal_info")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🧭 Направления студии Soul:\n\nВыберите направление, чтобы узнать подробнее:",
        reply_markup=reply_markup,
    )


# ----------------------------------------------------------------------
# Описания направлений
# ----------------------------------------------------------------------
async def show_bachata_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает описание Бачаты."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔙 Назад к направлениям", callback_data="directions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "💃 Бачата\n\n"
        "Бачата — это чувственный и романтичный парный танец "
        "родом из Доминиканской Республики. В нашей студии вы "
        "научитесь уверенно вести и чувствовать партнёра, "
        "импровизировать и получать удовольствие от движения.\n\n"
        "Подробнее на сайте: https://soul.by/bachata"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_tribal_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает описание Трайбла."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔙 Назад к направлениям", callback_data="directions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🔥 Трайбл\n\n"
        "Трайбл (Tribal Fusion) — это завораживающий стиль танца, "
        "сочетающий элементы восточных, индийских и фламенко-мотивов. "
        "Акцент на пластику, изоляцию и выразительность каждой детали.\n\n"
        "Подробнее на сайте: https://soul.by/tribal"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


# ----------------------------------------------------------------------
# /start
# ----------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    utm_source = context.args[0] if context.args else "direct"

    # Сохраняем пользователя (utm_source обновится, имя/телефон не затронутся)
    save_user(user.id, utm_source)

    # Приветствие с учётом источника
    welcome = f"Привет, {user.first_name}!"
    if utm_source == "bachata":
        welcome += "\n\nРады видеть вас на странице Бачаты! 💃"
    else:
        welcome += "\n\nДобро пожаловать в нашу студию танцев и йоги! 🧘"

    await show_main_menu(
        update, context, text=welcome + "\n\nВыберите действие в меню ниже:"
    )


# ----------------------------------------------------------------------
# /cancel — отмена любого диалога
# ----------------------------------------------------------------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет диалог и возвращает главное меню."""
    await show_main_menu(
        update, context, text="❌ Диалог отменён. Возвращаюсь в главное меню."
    )
    return ConversationHandler.END


# ----------------------------------------------------------------------
# ConversationHandler: запись на занятие
# ----------------------------------------------------------------------
async def book_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1 — запрашиваем имя."""
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "✍️ Давайте запишем вас на занятие!\n\n"
        "Шаг 1 из 2. Как вас зовут? (напишите имя и фамилию)\n\n"
        "Или нажмите /cancel чтобы отменить запись."
    )
    return FULL_NAME


async def book_full_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2 — сохраняем имя, запрашиваем телефон."""
    full_name = update.message.text.strip()
    if not full_name:
        await update.message.reply_text("Пожалуйста, напишите ваше имя.")
        return FULL_NAME

    context.user_data["full_name"] = full_name
    await update.message.reply_text(
        f"Отлично, {full_name}!\n\n"
        "Шаг 2 из 2. Укажите ваш номер телефона, чтобы мы могли с вами связаться:\n\n"
        "Или нажмите /cancel чтобы отменить запись."
    )
    return PHONE


async def book_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Финал — сохраняем телефон + имя в БД, уведомляем администратора."""
    phone = update.message.text.strip()
    if not phone:
        await update.message.reply_text("Пожалуйста, напишите ваш номер телефона.")
        return PHONE

    full_name = context.user_data.get("full_name", "")
    telegram_id = update.effective_user.id

    # Сохраняем в БД
    update_user_details(telegram_id, full_name, phone)

    # Уведомление администратору
    if ADMIN_CHAT_ID:
        admin_message = (
            f"✅ Новая запись на занятие!\n\n"
            f"Имя: {full_name}\n"
            f"Телефон: {phone}\n"
            f"Telegram ID: {telegram_id}\n"
            f"Username: @{update.effective_user.username or '—'}"
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=admin_message,
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление админу: {e}")

    # Показываем подтверждение и возвращаем главное меню
    await update.message.reply_text(
        f"✅ Вы записаны!\n\n"
        f"Имя: {full_name}\n"
        f"Телефон: {phone}\n\n"
        "Мы свяжемся с вами в ближайшее время для подтверждения."
    )
    await show_main_menu(update, context)
    return ConversationHandler.END


# ----------------------------------------------------------------------
# Обработчик инлайн-кнопок (все, кроме book, который перехватывается
# ConversationHandler)
# ----------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    # --- Подменю "Направления" ---
    if data == "directions":
        await show_directions_menu(update, context)
        return

    # --- Описания направлений ---
    if data == "bachata_info":
        await show_bachata_info(update, context)
        return

    if data == "tribal_info":
        await show_tribal_info(update, context)
        return

    # --- Возврат в главное меню ---
    if data == "back_to_main":
        await show_main_menu(update, context)
        return

    # --- Остальные пункты ---
    if data == "schedule":
        text = "📅 Расписание:\n\nПонедельник: 19:00 Бачата\nСреда: 20:00 Йога\nПятница: 19:00 Стретчинг"
    elif data == "prices":
        text = "💰 Абонементы:\n\nРазовое занятие — 500 руб.\n4 занятия — 1800 руб.\n8 занятий — 3200 руб.\nБезлимит на месяц — 6000 руб."
    elif data == "info":
        text = "📍 Адрес: ул. Танцевальная, д. 5\n⏰ Часы работы: 10:00–22:00\n📞 Телефон: +7 (999) 123-45-67"
    elif data == "support":
        text = "Напишите ваш вопрос, и мы ответим в ближайшее время.\n\n(Пока эта функция в разработке)"
    elif data == "book":
        # Этот случай обрабатывается ConversationHandler, но на случай
        # если он не сработал — покажем сообщение
        text = "✍️ Запись на занятие..."
        await query.edit_message_text(text)
        return
    else:
        text = "Неизвестная команда."

    # Под каждым информационным сообщением — кнопка возврата в главное меню
    back_keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")],
        ]
    )
    await query.edit_message_text(text, reply_markup=back_keyboard)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler для записи на занятие
    book_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(book_start, pattern="^book$")],
        states={
            FULL_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, book_full_name)
            ],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_phone)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(book_conv)
    application.add_handler(CallbackQueryHandler(button_handler))

    # Запуск в режиме polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
