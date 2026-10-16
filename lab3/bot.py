"""Точка входа Telegram-бота: личный ассистент по задачам и встречам."""

from __future__ import annotations

import asyncio
import logging
import sys

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    TypeHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, TIMEZONE_NAME
from db import init_db
from handlers import (
    WAITING_NAME,
    admin_cmd,
    admin_export_cmd,
    admin_invite_cmd,
    admin_stt_cmd,
    calendar_cmd,
    cancel_name,
    done_cmd,
    export_cmd,
    feedback_cmd,
    handle_document,
    handle_text,
    handle_voice,
    help_cmd,
    meetings_cmd,
    on_callback,
    receive_name,
    review_cmd,
    setname,
    settings_cmd,
    sheets_cmd,
    start,
    stats_cmd,
    survey_cmd,
    tasks_cmd,
    today_cmd,
    waiting_name_cmd,
    week_cmd,
)
from jobs import schedule

# В лог не пишем токены и ключи — только служебные события.
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
# httpx пишет в лог полный адрес запроса, а в адресе Telegram API есть токен бота.
# Поднимаем уровень, чтобы токен не попадал в консоль.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Необработанная ошибка", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Что-то сломалось на моей стороне. Попробуй ещё раз — если повторится, напиши /help."
            )
        except Exception:
            pass


# Список команд для кнопки «Меню» слева от поля ввода в Telegram.
# Только новые сообщения. Отредактированное сообщение приходит как edited_message,
# у него update.message = None — раньше это роняло обработчик (AttributeError в логах 16.10).
NEW_MSG = filters.UpdateType.MESSAGE

BOT_COMMANDS = [
    BotCommand("today", "Встречи и задачи на сегодня"),
    BotCommand("tasks", "Открытые задачи"),
    BotCommand("week", "План на неделю"),
    BotCommand("meetings", "Прошедшие встречи"),
    BotCommand("review", "Итоги недели для руководителя"),
    BotCommand("done", "Что сделано за неделю"),
    BotCommand("calendar", "Подключить календарь"),
    BotCommand("stats", "Моя статистика"),
    BotCommand("sheets", "Онлайн-таблица задач"),
    BotCommand("export", "Выгрузка в Excel"),
    BotCommand("settings", "Часовой пояс и уведомления"),
    BotCommand("feedback", "Оставить отзыв"),
    BotCommand("survey", "Короткий опрос о боте"),
    BotCommand("setname", "Как меня зовут на встречах"),
    BotCommand("help", "Что умеет бот"),
]


async def apply_user_timezone(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перед любой обработкой: включить часовой пояс этого пользователя (см. dates.get_tz)."""
    import storage
    from dates import set_current_tz

    user = update.effective_user if isinstance(update, Update) else None
    set_current_tz(storage.get_user_tz(user.id) if user else None)


async def post_init(application: Application) -> None:
    """После запуска: регистрируем команды, чтобы они появились в кнопке «Меню»."""
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        logger.exception("Не удалось установить меню команд")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("Не задан TELEGRAM_BOT_TOKEN в .env")
        sys.exit(1)

    init_db()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    name_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("setname", setname),
        ],
        states={
            WAITING_NAME: [
                MessageHandler(NEW_MSG & filters.TEXT & ~filters.COMMAND, receive_name),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_name),
            MessageHandler(filters.COMMAND, waiting_name_cmd),
        ],
        name="ask_name",
        persistent=False,
    )

    # group=-1 — выполняется раньше всех остальных обработчиков.
    application.add_handler(TypeHandler(Update, apply_user_timezone), group=-1)
    application.add_handler(name_conversation)
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("tasks", tasks_cmd))
    application.add_handler(CommandHandler("done", done_cmd))
    application.add_handler(CommandHandler("today", today_cmd))
    application.add_handler(CommandHandler("week", week_cmd))
    application.add_handler(CommandHandler("meetings", meetings_cmd))
    application.add_handler(CommandHandler("review", review_cmd))
    application.add_handler(CommandHandler("calendar", calendar_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("sheets", sheets_cmd))
    application.add_handler(CommandHandler("feedback", feedback_cmd))
    application.add_handler(CommandHandler("admin", admin_cmd))
    application.add_handler(CommandHandler("admin_export", admin_export_cmd))
    application.add_handler(CommandHandler("admin_invite", admin_invite_cmd))
    application.add_handler(CommandHandler("admin_stt", admin_stt_cmd))
    application.add_handler(CommandHandler("survey", survey_cmd))
    application.add_handler(CommandHandler("settings", settings_cmd))
    application.add_handler(MessageHandler(NEW_MSG & filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(NEW_MSG & (filters.VOICE | filters.AUDIO), handle_voice))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(NEW_MSG & filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(on_error)

    schedule(application)
    logger.info("Бот запускается, часовой пояс по умолчанию %s", TIMEZONE_NAME)
    # Python 3.14+ больше не создаёт цикл событий автоматически,
    # а python-telegram-bot 21 на это рассчитывает — создаём его вручную.
    asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
