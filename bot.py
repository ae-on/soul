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
    """Показывает подменю со всеми направлениями."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔥 Трайбл фьюжн", callback_data="tribal_info")],
        [InlineKeyboardButton("🩰 Восточный танец", callback_data="oriental_info")],
        [InlineKeyboardButton("🧘 Йога", callback_data="yoga_info")],
        [InlineKeyboardButton("💃 Бачата (для пар)", callback_data="bachata_info")],
        [InlineKeyboardButton("🌿 Цигун", callback_data="qigong_info")],
        [InlineKeyboardButton("🇮🇳 Индийский танец", callback_data="indian_info")],
        [InlineKeyboardButton("🏛️ Исторический танец", callback_data="historical_info")],
        [InlineKeyboardButton("🧘 Пилатес", callback_data="pilates_info")],
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
async def show_tribal_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔙 Назад к направлениям", callback_data="directions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🔥 Трайбл фьюжн (Tribal Fusion)\n\n"
        "Новый виток развития восточного танца. Это медитативность, "
        "поиск себя и искусное владение телом. Техника сочетает элементы "
        "восточного, испанского, индийского и других танцев, предоставляя "
        "большую свободу для самовыражения.\n\n"
        "Подробнее: http://soul.by/index.php/tribal/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_oriental_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔙 Назад к направлениям", callback_data="directions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🩰 Восточный танец (Беллиданс)\n\n"
        "Древнейшая танцевальная техника, которая помогает раскрыть "
        "женственность, мягкость и сексуальность. Учит находить баланс "
        "между напряжением и расслаблением, избавляет от последствий стресса.\n\n"
        "Подробнее: http://soul.by/index.php/vostocniy-tanec/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_yoga_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔙 Назад к направлениям", callback_data="directions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🧘 Йога\n\n"
        "Практика, направленная на оздоровление организма, поиск его "
        "скрытых возможностей и создание правильного умственного и "
        "эмоционального настроя. В студии преподают хатха-йогу, "
        "адаптированную к нуждам современного человека.\n\n"
        "Подробнее: http://soul.by/index.php/yoga-minsk/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_bachata_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔙 Назад к направлениям", callback_data="directions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "💃 Бачата (для пар)\n\n"
        "Бачата — это диалог тел, разговор без слов. В студии предлагают "
        "уникальный формат, где пары проходят весь путь обучения вместе, "
        "не меняя партнёров. Это идеальный способ укрепить отношения, "
        "раскрыть новый уровень доверия и гармонии, а также устроить "
        "необычное свидание. Обучение ведётся с нуля в комфортной и "
        "дружеской атмосфере.\n\n"
        "Подробнее: https://soul.by/bachata-minsk/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_qigong_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔙 Назад к направлениям", callback_data="directions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🌿 Цигун\n\n"
        "«Ци» — поток жизненной энергии, «Гун» — работа. Это система "
        "плавных, контролируемых движений, управления дыханием и вниманием, "
        "выросшая из китайской народной медицины. Занятия цигун наполняют "
        "энергией, дарят здоровье и долголетие, а также способствуют "
        "спокойствию ума и внутреннему равновесию.\n\n"
        "Подробнее: https://soul.by/cigun-minsk/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_indian_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔙 Назад к направлениям", callback_data="directions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🇮🇳 Индийский танец\n\n"
        "Индийский танец — это не просто искусство, а духовная практика, "
        "воплощающая древнюю мудрость. Подобно йоге, он способен погрузить "
        "танцора в состояние транса, раскрывая внутреннюю сущность. "
        "В студии преподают как классический индийский танец, так и "
        "современный и яркий стиль «Болливуд», а также другие стили: "
        "Гарба, Дандия, Лавани и Бхангра.\n\n"
        "Подробнее: https://soul.by/indian-dance-minsk/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_historical_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔙 Назад к направлениям", callback_data="directions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🏛️ Исторический танец\n\n"
        "Это прекрасная возможность погрузиться в миры любимых книг и "
        "фильмов, узнать много нового об истории и культуре, завести "
        "интересные знакомства. Значительную часть исторических танцев "
        "можно танцевать без особенной физической подготовки, ведь они "
        "были придуманы для того, чтобы совершенно обычные люди могли "
        "весело и интересно провести вечер. Вас ожидает: изучение схем "
        "танцев из исторических сборников, освоение нескольких видов "
        "вальса, участие в балах и пикниках, основы бального этикета.\n\n"
        "Подробнее: https://soul.by/historical-dance/"
    )
    await query.edit_message_text(text, reply_markup=reply_markup)


async def show_pilates_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔙 Назад к направлениям", callback_data="directions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🧘 Пилатес\n\n"
        "Пилатес — это уникальная система физических упражнений, которая "
        "направлена на оздоровление организма и восстановление естественного "
        "положения тела в пространстве. Это одна из наиболее щадящих форм "
        "воздействия на организм, разработанная для реабилитации людей "
        "после травм позвоночника, поэтому она не имеет практически "
        "никаких противопоказаний. Пилатес доступен людям любого уровня "
        "подготовки. Занятия включают работу с основными принципами "
        "пилатеса: концентрация, мышечный контроль, правильное дыхание, "
        "плавность движений, а также проработку глубинных мышц, "
        "служащих «каркасом» для позвоночника.\n\n"
        "Подробнее: https://soul.by/pilates/"
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
    if data == "tribal_info":
        await show_tribal_info(update, context)
        return
    if data == "oriental_info":
        await show_oriental_info(update, context)
        return
    if data == "yoga_info":
        await show_yoga_info(update, context)
        return
    if data == "bachata_info":
        await show_bachata_info(update, context)
        return
    if data == "qigong_info":
        await show_qigong_info(update, context)
        return
    if data == "indian_info":
        await show_indian_info(update, context)
        return
    if data == "historical_info":
        await show_historical_info(update, context)
        return
    if data == "pilates_info":
        await show_pilates_info(update, context)
        return

    # --- Возврат в главное меню ---
    if data == "back_to_main":
        await show_main_menu(update, context)
        return

    # --- Остальные пункты ---
    if data == "schedule":
        text = (
            "📅 Расписание:\n\n"
            "Понедельник: 19:00 Бачата\n"
            "Среда: 20:00 Йога\n"
            "Пятница: 19:00 Стретчинг"
        )
    elif data == "prices":
        text = (
            "💰 СТОИМОСТЬ АБОНЕМЕНТОВ:\n\n"
            "▫️ 25 / 30 руб — Разовое (1 / 1.5 часа)\n"
            "▫️ 90 / 110 руб — 4 занятия (1 / 1.5 часа)\n"
            "▫️ 145 / 185 руб — 8 занятий (1 / 1.5 часа)\n"
            "▫️ 185 / 235 руб — 12 занятий (1 / 1.5 часа)"
        )
    elif data == "info":
        text = (
            "📍 О СТУДИИ\n\n"
            "📞 Телефоны:\n"
            "Тел.: +375 (29) 351 23 61\n"
            "Тел.: +375 (29) 751 23 61\n\n"
            "🏠 Адреса:\n"
            "Шорная 20-12Н — [soul.by/zal1/](https://soul.by/zal1/)\n"
            "Шорная 20-4Н — [soul.by/zal2/](https://soul.by/zal2/)\n\n"
            "🗺️ Карта:\n"
            "[Открыть на Яндекс.Картах](https://yandex.by/maps/?ll=27.54178400,53.90070400&z=17)"
        )
    elif data == "support":
        text = (
            "💬 Задать вопрос\n\n"
            "Напишите ваш вопрос, и мы ответим в ближайшее время.\n\n"
            "(Пока эта функция в разработке)"
        )
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
