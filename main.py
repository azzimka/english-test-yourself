import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher

from config import BOT_TOKEN, WEB_PORT, WEB_HOST, WEBAPP_URL
from database import init_db
from handlers import admin_router, common_router, student_router
from services.web_server import create_web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

async def main():
    logger.info("Initializing database...")
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # Register routers
    dp.include_router(admin_router)
    dp.include_router(common_router)
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
