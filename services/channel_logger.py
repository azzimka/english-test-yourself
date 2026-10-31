import html
import logging
from datetime import datetime
from aiogram import Bot, types
from aiogram.types import BufferedInputFile
from config import LOG_CHANNEL_ID

logger = logging.getLogger(__name__)


def format_size(size_bytes: int) -> str:
    """Format bytes into human-readable size string."""
    if size_bytes < 1024:
        return f"{size_bytes} Б"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} КБ"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} МБ"


async def log_test_material_upload(
    bot: Bot,
    user: types.User,
    custom_title: str | None,
    test_type: str,
    message: types.Message,
    text_content: str | None = None,
) -> None:
    """
    Forwards or sends uploaded test materials (document or text)
    to the audit channel along with teacher metadata.
    """
    if not LOG_CHANNEL_ID:
        return

    try:
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        teacher_name = html.escape(user.full_name or "Учитель")
        user_handle = f"@{user.username}" if user.username else f"<code>ID: {user.id}</code>"
        type_name = "🏆 Олимпиадный тест" if test_type == "olympiad" else "📚 Обычный тест"
        title_str = html.escape(custom_title) if custom_title else "<i>(без названия)</i>"

        if message.document:
            file_name = message.document.file_name or "документ"
            file_size_str = format_size(message.document.file_size or 0)
            caption = (
                "📁 <b>Новый материал для теста</b>\n\n"
                f"👨‍🏫 <b>Учитель:</b> {teacher_name} ({user_handle})\n"
                f"📚 <b>Категория:</b> {type_name}\n"
                f"📌 <b>Название теста:</b> «{title_str}»\n"
                f"📄 <b>Файл:</b> <code>{html.escape(file_name)}</code> ({file_size_str})\n"
                f"⏱ <b>Время загрузки:</b> {now_str}"
            )
            await bot.send_document(
                chat_id=LOG_CHANNEL_ID,
                document=message.document.file_id,
                caption=caption,
                parse_mode="HTML"
            )
        elif message.text:
            raw_text = message.text.strip()
            caption = (
                "📝 <b>Новый материал для теста (Текст)</b>\n\n"
                f"👨‍🏫 <b>Учитель:</b> {teacher_name} ({user_handle})\n"
                f"📚 <b>Категория:</b> {type_name}\n"
                f"📌 <b>Название теста:</b> «{title_str}»\n"
                f"📄 <b>Формат:</b> Текстовое сообщение ({len(raw_text)} симв.)\n"
                f"⏱ <b>Время загрузки:</b> {now_str}"
            )
            if len(raw_text) <= 2500:
                full_text = f"{caption}\n\n📋 <b>Текст заданий:</b>\n<blockquote>{html.escape(raw_text)}</blockquote>"
                await bot.send_message(
                    chat_id=LOG_CHANNEL_ID,
                    text=full_text,
                    parse_mode="HTML"
                )
            else:
                safe_name = "".join(c for c in (custom_title or "materials") if c.isalnum() or c in ("-", "_", " ")).strip()
                filename = f"{safe_name[:30] or 'test_materials'}.txt"
                file_data = BufferedInputFile(
                    raw_text.encode("utf-8"),
                    filename=filename
                )
                await bot.send_document(
                    chat_id=LOG_CHANNEL_ID,
                    document=file_data,
                    caption=caption,
                    parse_mode="HTML"
                )
        logger.info("Successfully sent upload notification to LOG_CHANNEL_ID (%s)", LOG_CHANNEL_ID)
    except Exception as e:
        logger.warning("Could not send material to LOG_CHANNEL_ID (%s): %s", LOG_CHANNEL_ID, e)


async def log_test_published(
    bot: Bot,
    user: types.User,
    title: str,
    test_type: str,
    question_count: int,
    has_answer_keys: Optional[bool] = None,
) -> None:
    """Logs successful test publication to the audit channel."""
    if not LOG_CHANNEL_ID:
        return
    try:
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        teacher_name = html.escape(user.full_name or "Учитель")
        user_handle = f"@{user.username}" if user.username else f"<code>ID: {user.id}</code>"
        type_name = "🏆 Олимпиадный тест" if test_type == "olympiad" else "📚 Обычный тест"
        title_str = html.escape(title)

        keys_info = ""
        if has_answer_keys is not None:
            if has_answer_keys:
                keys_info = "\n🔑 <b>Ключи к ответам:</b> найдены в материале"
            else:
                keys_info = "\n⚠️ <b>Ключи к ответам:</b> не найдены (требуется проверка учителем)"

        text = (
            "✅ <b>Тест успешно опубликован!</b>\n\n"
            f"📌 <b>Название:</b> «{title_str}»\n"
            f"📚 <b>Категория:</b> {type_name}\n"
            f"🔢 <b>Количество заданий:</b> {question_count}{keys_info}\n"
            f"👨‍🏫 <b>Учитель:</b> {teacher_name} ({user_handle})\n"
            f"⏱ <b>Время:</b> {now_str}"
        )
        await bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=text,
            parse_mode="HTML"
        )
        logger.info("Successfully sent publish notification to LOG_CHANNEL_ID (%s)", LOG_CHANNEL_ID)
    except Exception as e:
        logger.warning("Could not send publish log to LOG_CHANNEL_ID (%s): %s", LOG_CHANNEL_ID, e)


async def log_test_parse_failed(
    bot: Bot,
    user: types.User,
    custom_title: str | None,
    test_type: str,
    error_message: str,
) -> None:
    """Logs test parsing failure to the audit channel."""
    if not LOG_CHANNEL_ID:
        return
    try:
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        teacher_name = html.escape(user.full_name or "Учитель")
        user_handle = f"@{user.username}" if user.username else f"<code>ID: {user.id}</code>"
        type_name = "🏆 Олимпиадный тест" if test_type == "olympiad" else "📚 Обычный тест"
        title_str = html.escape(custom_title) if custom_title else "<i>(без названия)</i>"

        text = (
            "⚠️ <b>Ошибка при обработке теста нейросетью</b>\n\n"
            f"👨‍🏫 <b>Учитель:</b> {teacher_name} ({user_handle})\n"
            f"📚 <b>Категория:</b> {type_name}\n"
            f"📌 <b>Название:</b> «{title_str}»\n"
            f"❌ <b>Ошибка:</b> <code>{html.escape(error_message[:400])}</code>\n"
            f"⏱ <b>Время:</b> {now_str}"
        )
        await bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=text,
            parse_mode="HTML"
        )
        logger.info("Successfully sent failure notification to LOG_CHANNEL_ID (%s)", LOG_CHANNEL_ID)
    except Exception as e:
        logger.warning("Could not send fail log to LOG_CHANNEL_ID (%s): %s", LOG_CHANNEL_ID, e)
