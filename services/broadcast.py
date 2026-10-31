import logging
import aiosqlite
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMIN_IDS
from database import has_broadcast_been_sent, mark_broadcast_as_sent
from database.db import DB_PATH
from keyboards.admin_kb import get_admin_main_kb

logger = logging.getLogger(__name__)

BROADCAST_V2_ID = "v2_release_notification"

V2_ANNOUNCEMENT_TEXT = (
    "🎉 <b>English Bot 2.0!</b>\n\n"
    "<b>Что нового в версии 2.0:</b>\n\n"
    "• 🎓 <b>Выдача сертификатов:</b> отправляйте дипломы ученикам в 1 клик прямо в боте.\n"
    "• 🖼 <b>Иллюстрации в тестах:</b> бот теперь автоматически переносит схемы и графики из PDF."
)


async def notify_teachers_v2_release(bot: Bot):
    """
    Sends the v2.0 release announcement to all teachers/admins exactly once.
    Safely recorded in system_broadcasts to guarantee zero re-sends on future restarts/pushes.
    """
    try:
        if await has_broadcast_been_sent(BROADCAST_V2_ID):
            logger.info("V2 release announcement was already sent previously. Skipping broadcast.")
            return

        logger.info("Preparing v2 release announcement for teachers...")
        teacher_ids = set(ADMIN_IDS)

        # Also collect any teachers registered in users table
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT telegram_id FROM users WHERE role IN ('teacher', 'admin')") as cur:
                    rows = await cur.fetchall()
                    for r in rows:
                        if r["telegram_id"]:
                            teacher_ids.add(r["telegram_id"])
        except Exception as e:
            logger.debug("Could not query additional teachers from users table: %s", e)

        if not teacher_ids:
            logger.warning("No teacher/admin IDs found to send v2 announcement.")
            await mark_broadcast_as_sent(BROADCAST_V2_ID)
            return

        inline_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Начать работу 🔥", callback_data="admin_open_main_menu")]
        ])

        sent_count = 0
        for tid in teacher_ids:
            try:
                reply_kb = get_admin_main_kb(teacher_id=tid)
                await bot.send_message(
                    chat_id=tid,
                    text=V2_ANNOUNCEMENT_TEXT,
                    parse_mode="HTML",
                    reply_markup=inline_kb
                )
                sent_count += 1
                logger.info("Delivered v2 announcement to teacher ID: %s", tid)
            except Exception as e:
                logger.warning("Could not deliver v2 announcement to teacher ID %s: %s", tid, e)

        # Mark as sent permanently in DB
        await mark_broadcast_as_sent(BROADCAST_V2_ID)
        logger.info("V2 release broadcast completed. Successfully delivered to %d teachers.", sent_count)
    except Exception as e:
        logger.error("Error running notify_teachers_v2_release: %s", e)
