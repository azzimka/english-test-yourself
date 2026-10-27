import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat

from config import BOT_TOKEN, WEB_PORT, WEB_HOST, WEBAPP_URL, ADMIN_IDS
from database import init_db
from handlers import admin_router, common_router, student_router
from services.web_server import create_web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

async def setup_bot_ui(bot: Bot):
    """Configures bot commands menu and welcome description for first-time users."""
    commands = [
        BotCommand(command="start", description="🚀 Главное меню"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        logger.info("Bot commands menu configured with /start only.")
    except Exception as e:
        logger.warning("Could not set default bot commands: %s", e)

    # Clean up any previously set chat-specific commands so everyone sees only /start
    for admin_id in ADMIN_IDS:
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=admin_id))
            logger.info("Cleared custom chat commands for admin %s", admin_id)
        except Exception as e:
            logger.debug("Could not delete custom commands for %s: %s", admin_id, e)

    desc = (
        "🇬🇧 English Lab\n\n"
        "👩‍🏫 Учитель: загружает тесты и утверждает оценки.\n"
        "🎓 Ученик: решает задания и получает результат."
    )
    for lang in [None, "ru"]:
        try:
            await bot.set_my_description(description=desc, language_code=lang)
        except Exception as e:
            logger.warning("Could not set bot description for %s: %s", lang, e)

    short_desc = "Школьные тесты и олимпиады по английскому языку."
    for lang in [None, "ru"]:
        try:
            await bot.set_my_short_description(short_description=short_desc, language_code=lang)
        except Exception as e:
            logger.warning("Could not set bot short description for %s: %s", lang, e)

async def main():
    logger.info("Initializing database...")
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    logger.info("Setting up bot commands and welcome screens...")
    await setup_bot_ui(bot)

    # Register routers (common_router MUST be first to handle /start and reset states unconditionally)
    dp.include_router(common_router)
    dp.include_router(admin_router)
    dp.include_router(student_router)

    # Setup Mini App web server
    app = create_web_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=WEB_HOST, port=WEB_PORT)
    await site.start()
    logger.info("Mini App web server running at http://%s:%s", WEB_HOST, WEB_PORT)
    if WEBAPP_URL:
        logger.info("Mini App public URL: %s", WEBAPP_URL)
    else:
        logger.info("WEBAPP_URL is not set in .env. Bot will use chat mode or tunnel URL.")

    logger.info("Deleting webhook and starting polling...")
    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
