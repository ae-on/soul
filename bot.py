import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import os
from dotenv import load_dotenv
from database import init_db, save_user

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)

# Инициализируем БД при старте
init_db()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Получаем параметр после /start (например, "bachata")
    utm_source = context.args[0] if context.args else "direct"

    # Сохраняем пользователя (пока без имени и телефона, их запросим позже)
    save_user(user.id, utm_source)

    # Главное меню
    keyboard = [
        [InlineKeyboardButton("📅 Расписание и запись", callback_data='schedule')],
        [InlineKeyboardButton("💰 Абонементы и цены", callback_data='prices')],
        [InlineKeyboardButton("📍 О студии", callback_data='info')],
        [InlineKeyboardButton("💬 Задать вопрос", callback_data='support')],
        [InlineKeyboardButton("✍️ Записаться на занятие", callback_data='book')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Приветствие с учётом источника
    welcome = f"Привет, {user.first_name}!"
    if utm_source == "bachata":
        welcome += "\n\nРады видеть вас на странице Бачаты! 💃"
    else:
        welcome += "\n\nДобро пожаловать в нашу студию танцев и йоги! 🧘"
    welcome += "\n\nВыберите действие в меню ниже:"

    await update.message.reply_text(welcome, reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == 'schedule':
        text = "📅 Расписание:\n\nПонедельник: 19:00 Бачата\nСреда: 20:00 Йога\nПятница: 19:00 Стретчинг"
    elif data == 'prices':
        text = "💰 Абонементы:\n\nРазовое занятие — 500 руб.\n4 занятия — 1800 руб.\n8 занятий — 3200 руб.\nБезлимит на месяц — 6000 руб."
    elif data == 'info':
        text = "📍 Адрес: ул. Танцевальная, д. 5\n⏰ Часы работы: 10:00–22:00\n📞 Телефон: +7 (999) 123-45-67"
    elif data == 'support':
        text = "Напишите ваш вопрос, и мы ответим в ближайшее время.\n\n(Пока эта функция в разработке)"
    elif data == 'book':
        text = "✍️ Запись на занятие:\n\nНапишите ваше имя и телефон, и мы свяжемся с вами для подтверждения."
        # Здесь можно попросить пользователя ввести контактные данные
        # либо перейти в отдельный диалог (это уже следующий шаг)
    else:
        text = "Неизвестная команда."

    await query.edit_message_text(text, reply_markup=None)  # убираем меню после выбора

def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Запуск в режиме polling (удобно для локальной отладки)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
