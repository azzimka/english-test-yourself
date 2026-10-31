import html
import logging
import asyncio
from io import BytesIO
from typing import Optional, List, Dict, Any, Tuple
from aiogram import Router, types, F, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramForbiddenError, TelegramAPIError
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMIN_IDS, WEBAPP_URL
from database import (
    create_test_with_questions,
    get_tests,
    get_test_by_id,
    update_test_title,
    delete_test,
    get_test_questions,
    get_test_submissions,
    get_user_by_id,
    get_distinct_student_classes,
    get_students_by_class,
)
from services import (
    extract_text_from_file,
    extract_file_content_and_images,
    UnsupportedFileError,
    FileExtractionError,
    parse_test_content,
    ParsedTest,
)
from services.channel_logger import (
    log_test_material_upload,
    log_test_published,
    log_test_parse_failed,
)
from keyboards.admin_kb import (
    get_admin_main_kb,
    get_test_type_choice_kb,
    get_title_choice_kb,
    get_confirm_test_kb,
    get_admin_tests_kb,
    get_admin_test_actions_kb,
    get_cancel_rename_kb,
    get_cert_classes_kb,
    get_cert_students_kb,
    get_cert_search_prompt_kb,
    get_cert_no_results_kb,
    get_cert_empty_class_kb,
    get_cert_cancel_kb,
)
from states.admin_states import UploadTestStates, CertificateStates

logger = logging.getLogger(__name__)
router = Router()

# Фильтр: только для администраторов из ADMIN_IDS
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


LOADING_STEPS = [
    "📖 <b>Читаю и изучаю ваш документ...</b>",
    "🔍 <b>Нахожу все задания и варианты ответов...</b>",
    "🧠 <b>Разбираю грамматику и проверяю правильные ключи...</b>",
    "✍️ <b>Формирую интерактивный тест для учеников...</b>",
    "✨ <b>Почти готово, навожу финальный лоск...</b>",
    "🚀 <b>Финальная проверка структуры заданий...</b>",
    "⏳ <b>Завершаю обработку и открываю предпросмотр...</b>",
]


async def animate_loading_status(status_msg: types.Message, bot: Bot, stop_event: asyncio.Event):
    """
    Динамически обновляет статус в Telegram каждые ~2.2 секунды,
    пока идет обработка теста, создавая живой и дружелюбный отклик.
    """
    idx = 0
    try:
        await bot.send_chat_action(chat_id=status_msg.chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2.2)
            break
        except asyncio.TimeoutError:
            pass

        if stop_event.is_set():
            break

        idx = (idx + 1) % len(LOADING_STEPS)
        new_text = LOADING_STEPS[idx]
        try:
            await status_msg.edit_text(new_text, parse_mode="HTML")
            await bot.send_chat_action(chat_id=status_msg.chat.id, action=ChatAction.TYPING)
        except TelegramRetryAfter as e:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=e.retry_after)
                break
            except asyncio.TimeoutError:
                pass
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                logger.debug("TelegramBadRequest in animate_loading_status: %s", e)
        except Exception as e:
            logger.debug("Exception in animate_loading_status: %s", e)


# --- 1. Меню "Создать тест" ---
@router.message(Command("create_test"))
@router.message(F.text == "📝 Создать тест")
@router.callback_query(F.data == "nav_create_test")
async def start_create_test(event: types.Message | types.CallbackQuery, state: FSMContext):
    if isinstance(event, types.CallbackQuery):
        try:
            await event.answer()
        except TelegramBadRequest:
            pass
    await state.clear()
    msg = event if isinstance(event, types.Message) else event.message

    await msg.answer(
        "📝 <b>Создание нового теста</b>\n\n"
        "Выберите категорию теста:",
        reply_markup=get_test_type_choice_kb(),
        parse_mode="HTML"
    )
    await state.set_state(UploadTestStates.waiting_for_test_type)


@router.callback_query(F.data.startswith("set_test_type:"))
async def process_test_type_choice(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    test_type = callback.data.split(":")[1]
    await state.update_data(test_type=test_type)
    
    type_name = "Олимпиадный тест" if test_type == "olympiad" else "Обычный тест"
    try:
        await callback.message.edit_text(
            f"Выбрана категория: <b>{type_name}</b>\n\n"
            "✍️ <b>Как назовем этот тест?</b>\n"
            "Напишите название сообщением (например: <i>Unit 4: Conditionals</i> или <i>Школьная олимпиада 10 класс</i>):",
            reply_markup=get_title_choice_kb(),
            parse_mode="HTML"
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await state.set_state(UploadTestStates.waiting_for_title)


@router.callback_query(F.data == "skip_title", UploadTestStates.waiting_for_title)
async def process_skip_title(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Пожалуйста, напишите название теста сообщением в чат.", show_alert=True)


@router.message(UploadTestStates.waiting_for_title, F.text, ~F.text.startswith("/"))
async def process_custom_title(message: types.Message, state: FSMContext):
    title = message.text.strip()
    await state.update_data(custom_title=title)
    await ask_for_test_content(message, state, is_edit=False)


async def ask_for_test_content(message: types.Message, state: FSMContext, is_edit: bool = False):
    data = await state.get_data()
    title = data.get("custom_title")
    title_str = f"«{html.escape(title)}»" if title else "<i>(без названия)</i>"

    text = (
        f"📌 Название теста: <b>{title_str}</b>\n\n"
        "📥 <b>Теперь отправьте материалы теста:</b>\n"
        "• Документ <b>PDF (.pdf)</b> или <b>Word (.docx)</b>\n"
        "• Текстовый файл <b>(.txt)</b>\n"
        "• Или обычный текст заданий сообщением в чат\n\n"
        "⚡ Нейросеть мгновенно распознает вопросы, варианты ответов и правильные ключи."
    )
    if is_edit:
        await message.edit_text(text, parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")
    await state.set_state(UploadTestStates.waiting_for_content)


# --- 2. Обработка загружаемого контента (документы PDF, DOCX, TXT или текст) ---

# Если прислали фото в режиме загрузки теста — выводим вежливое предупреждение
@router.message(UploadTestStates.waiting_for_content, F.photo)
async def process_photo_rejected(message: types.Message):
    await message.answer(
        "📷 <b>Анализ фотографий не поддерживается</b>\n\n"
        "Бот работает с электронными документами. Пожалуйста, отправьте файл в формате:\n"
        "• <b>PDF (.pdf)</b>\n"
        "• <b>Word (.docx)</b>\n"
        "• <b>Текстовый файл (.txt)</b>\n"
        "• Или просто скопируйте текст задания в сообщение чата.",
        parse_mode="HTML"
    )


async def safe_edit_or_answer(
    message: types.Message,
    status_msg: types.Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None
) -> None:
    """Edits status_msg, or falls back to message.answer if editing fails."""
    try:
        await status_msg.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        logger.debug("Failed to edit status_msg (%s), falling back to message.answer", e)
        try:
            await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            pass
    except Exception:
        try:
            await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            pass


@router.message(UploadTestStates.waiting_for_content, (F.text & ~F.text.startswith("/")) | F.document)
async def process_test_upload(message: types.Message, state: FSMContext, bot: Bot):
    if message.text:
        text_content = message.text.strip()
        if len(text_content) < 10:
            await message.answer(
                "❌ Текст задания слишком короткий. Пожалуйста, отправьте полные материалы теста или документ.",
                parse_mode="HTML"
            )
            return

    try:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    except Exception:
        pass

    # Запускаем живую динамическую анимацию статуса сразу
    status_msg = await message.answer(
        LOADING_STEPS[0],
        parse_mode="HTML"
    )
    stop_event = asyncio.Event()
    anim_task = asyncio.create_task(animate_loading_status(status_msg, bot, stop_event))

    data = await state.get_data()
    test_type = data.get("test_type", "regular")
    custom_title = data.get("custom_title")

    text_content = None
    extracted_images = []

    try:
        if message.text:
            text_content = message.text.strip()
        elif message.document:
            file_name = message.document.file_name or ""
            file_io = BytesIO()
            await bot.download(message.document, destination=file_io)
            file_bytes = file_io.getvalue()

            text_content, extracted_images = await asyncio.to_thread(
                extract_file_content_and_images, file_name, file_bytes
            )
    except (UnsupportedFileError, FileExtractionError) as file_err:
        stop_event.set()
        anim_task.cancel()
        try:
            await anim_task
        except (Exception, asyncio.CancelledError):
            pass
        await safe_edit_or_answer(message, status_msg, str(file_err))
        return
    except Exception as e:
        logger.exception("Error extracting file content")
        stop_event.set()
        anim_task.cancel()
        try:
            await anim_task
        except (Exception, asyncio.CancelledError):
            pass
        await safe_edit_or_answer(
            message,
            status_msg,
            f"❌ <b>Ошибка при чтении файла:</b>\n<code>{html.escape(str(e))}</code>"
        )
        return

    if not text_content or len(text_content.strip()) < 10:
        stop_event.set()
        anim_task.cancel()
        try:
            await anim_task
        except (Exception, asyncio.CancelledError):
            pass
        await safe_edit_or_answer(
            message,
            status_msg,
            "❌ Содержимое файла слишком короткое. Пожалуйста, отправьте полные материалы теста."
        )
        return

    # Асинхронно отправляем копию материалов в канал логирования (не блокируя парсинг)
    asyncio.create_task(
        log_test_material_upload(
            bot=bot,
            user=message.from_user,
            custom_title=custom_title,
            test_type=test_type,
            message=message,
            text_content=text_content
        )
    )

    try:
        # Запрос к нейросети (с передачей извлеченных изображений)
        parsed_test: ParsedTest = await parse_test_content(
            text_content=text_content,
            test_type=test_type,
            images=extracted_images
        )
    except Exception as e:
        logger.exception("Error processing uploaded test")
        asyncio.create_task(
            log_test_parse_failed(
                bot=bot,
                user=message.from_user,
                custom_title=custom_title,
                test_type=test_type,
                error_message=str(e)
            )
        )
        stop_event.set()
        anim_task.cancel()
        try:
            await anim_task
        except (Exception, asyncio.CancelledError):
            pass
        await safe_edit_or_answer(
            message,
            status_msg,
            f"❌ <b>Произошла ошибка при обработке теста:</b>\n<code>{html.escape(str(e))}</code>"
        )
        return
    finally:
        stop_event.set()
        anim_task.cancel()
        try:
            await anim_task
        except (Exception, asyncio.CancelledError):
            pass

    if not parsed_test or not parsed_test.questions:
        await safe_edit_or_answer(
            message,
            status_msg,
            "⚠️ Не удалось обнаружить вопросы в предоставленных материалах. Проверьте файл и попробуйте снова."
        )
        return

    # Если учитель заранее указал название, сохраняем его
    custom_title = data.get("custom_title")
    if custom_title:
        parsed_test.title = custom_title

    # Сохраняем в FSM для подтверждения
    await state.update_data(parsed_test=parsed_test.model_dump())
    await render_test_preview(status_msg, parsed_test.model_dump(), test_type, state)


# Если прислан другой тип медиа (голос, видео, стикер)
@router.message(UploadTestStates.waiting_for_content)
async def process_unsupported_media_upload(message: types.Message):
    await message.answer(
        "❌ <b>Неподдерживаемый тип сообщения</b>\n\n"
        "Пожалуйста, отправьте документ в формате <b>PDF</b>, <b>Word (.docx)</b>, текстовый файл <b>(.txt)</b> или текст заданий сообщением.",
        parse_mode="HTML"
    )


async def render_test_preview(message: types.Message, parsed_dict: dict, test_type: str, state: FSMContext):
    type_name = "Олимпиадный" if test_type == "olympiad" else "Обычный"
    questions = parsed_dict.get("questions", [])
    raw_title = parsed_dict.get("title") or "English Test"
    title_escaped = html.escape(str(raw_title))

    preview_lines = [
        "✅ <b>Тест успешно распознан!</b>\n",
        f"📌 <b>Название:</b> {title_escaped}",
        f"📊 <b>Всего вопросов:</b> {len(questions)}",
        f"🏷 <b>Категория:</b> {type_name}\n",
        "Опубликовать этот тест для учеников?"
    ]

    text = "\n".join(preview_lines)
    kb = get_confirm_test_kb()
    try:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.debug("render_test_preview edit failed: %s", e)
            try:
                await message.answer(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
    except Exception:
        try:
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    await state.set_state(UploadTestStates.waiting_for_confirmation)


# --- 2.3 Обработка случайной отправки файла вне сценария создания теста ---
@router.message(StateFilter(None), F.document)
async def handle_unexpected_file_upload(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state in (UploadTestStates.waiting_for_content, CertificateStates.waiting_for_certificate):
        return

    await message.answer(
        "📄 <b>Я вижу ваш документ!</b>\n\n"
        "Чтобы загрузить его в базу, нажмите кнопку <b>«📝 Создать тест»</b> в меню внизу, "
        "выберите категорию и отправьте файл повторно.",
        parse_mode="HTML"
    )


@router.message(StateFilter(None), F.photo)
async def handle_unexpected_photo_upload(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state in (UploadTestStates.waiting_for_content, CertificateStates.waiting_for_certificate):
        return

    await message.answer(
        "📷 <b>Анализ фотографий не поддерживается</b>\n\n"
        "Бот работает с электронными документами <b>PDF (.pdf)</b>, <b>Word (.docx)</b> или текстом сообщений.",
        parse_mode="HTML"
    )


@router.callback_query(F.data == "edit_title_btn", UploadTestStates.waiting_for_confirmation)
async def start_editing_title(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    await callback.message.reply("✍️ Напишите новое название для этого теста сообщением:")
    await state.set_state(UploadTestStates.editing_title)


@router.message(UploadTestStates.editing_title, F.text, ~F.text.startswith("/"))
async def process_new_title(message: types.Message, state: FSMContext):
    new_title = message.text.strip()
    data = await state.get_data()
    parsed_dict = data.get("parsed_test", {})
    test_type = data.get("test_type", "regular")

    parsed_dict["title"] = new_title
    await state.update_data(parsed_test=parsed_dict)

    preview_msg = await message.answer("🔄 Обновляю карточку...")
    await render_test_preview(preview_msg, parsed_dict, test_type, state)


# --- 3. Подтверждение публикации ---
@router.callback_query(F.data == "confirm_publish", UploadTestStates.waiting_for_confirmation)
async def confirm_publish_test(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    data = await state.get_data()
    parsed_dict = data.get("parsed_test")
    test_type = data.get("test_type", "regular")

    if not parsed_dict:
        try:
            await callback.message.edit_text("❌ Данные теста устарели. Пожалуйста, начните заново.")
        except TelegramBadRequest:
            pass
        await state.clear()
        return

    test_id = await create_test_with_questions(
        title=parsed_dict.get("title", "Тест по английскому"),
        test_type=test_type,
        created_by=callback.from_user.id,
        questions=parsed_dict.get("questions", []),
        description=parsed_dict.get("description", "")
    )
    has_answer_keys = bool(parsed_dict.get("has_answer_keys", False))
    # Асинхронно логируем публикацию теста в канал аудита
    asyncio.create_task(
        log_test_published(
            bot=callback.bot,
            user=callback.from_user,
            title=parsed_dict.get("title", "Тест по английскому"),
            test_type=test_type,
            question_count=len(parsed_dict.get("questions", [])),
            has_answer_keys=has_answer_keys,
        )
    )
    await state.clear()

    type_name = "Олимпиадные тесты" if test_type == "olympiad" else "Обычные тесты"
    publish_text = (
        "🎉 <b>Тест опубликован!</b>\n\n"
        f"Ученики уже могут найти его в разделе <b>«{html.escape(type_name)}»</b> и приступить к решению."
    )

    try:
        await callback.message.edit_text(
            publish_text,
            parse_mode="HTML"
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


# --- 4. Меню "Мои тесты" ---
@router.message(Command("my_tests"))
@router.message(F.text == "📂 Мои тесты")
@router.callback_query(F.data == "admin_tests_list")
async def list_admin_tests(event: types.Message | types.CallbackQuery):
    if isinstance(event, types.CallbackQuery):
        try:
            await event.answer()
        except TelegramBadRequest:
            pass

    tests = await get_tests(only_active=False)
    if not tests:
        text = "У вас пока нет созданных тестов. Нажмите <b>«📝 Создать тест»</b>, чтобы добавить первый!"
        if isinstance(event, types.CallbackQuery):
            try:
                await event.message.edit_text(text, parse_mode="HTML")
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise
        else:
            await event.answer(text, parse_mode="HTML")
        return

    text = "📂 <b>Список ваших тестов:</b>\n\nНажмите на тест для управления:"
    kb = get_admin_tests_kb(tests)
    if isinstance(event, types.CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("manage_test:"))
async def manage_single_test(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    await state.clear()
    test_id = int(callback.data.split(":")[1])
    test = await get_test_by_id(test_id)
    if not test:
        try:
            await callback.message.answer("⚠️ Тест не найден", parse_mode="HTML")
        except TelegramBadRequest:
            pass
        return

    questions = await get_test_questions(test_id)
    submissions = await get_test_submissions(test_id)

    type_str = "Олимпиадный" if test["test_type"] == "olympiad" else "Обычный"

    text = (
        f"📋 <b>Управление тестом:</b>\n\n"
        f"📌 <b>Название:</b> {html.escape(test['title'])}\n"
        f"🏷 <b>Тип:</b> {html.escape(type_str)}\n"
        f"📊 <b>Вопросов:</b> {len(questions)}\n"
        f"👥 <b>Сдали учеников:</b> {len(submissions)}"
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=get_admin_test_actions_kb(test_id),
            parse_mode="HTML"
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


@router.callback_query(F.data.startswith("rename_test:"))
async def start_renaming_test(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    test_id = int(callback.data.split(":")[1])
    test = await get_test_by_id(test_id)
    if not test:
        try:
            await callback.message.answer("⚠️ Тест не найден", parse_mode="HTML")
        except TelegramBadRequest:
            pass
        return

    await state.update_data(renaming_test_id=test_id)
    await state.set_state(UploadTestStates.renaming_existing_test)

    text = (
        f"✏️ <b>Переименование теста</b>\n\n"
        f"Текущее название: <i>«{html.escape(test['title'])}»</i>\n\n"
        f"Отправьте новое название сообщением в чат:"
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=get_cancel_rename_kb(test_id),
            parse_mode="HTML"
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


@router.message(UploadTestStates.renaming_existing_test, F.text, ~F.text.startswith("/"))
async def process_rename_test(message: types.Message, state: FSMContext):
    new_title = message.text.strip()
    data = await state.get_data()
    test_id = data.get("renaming_test_id")
    await state.clear()

    if not test_id:
        await message.answer("❌ Ошибка: тест не выбран.", parse_mode="HTML")
        return

    await update_test_title(test_id, new_title)
    test = await get_test_by_id(test_id)
    questions = await get_test_questions(test_id)
    submissions = await get_test_submissions(test_id)

    type_str = "Олимпиадный" if test and test["test_type"] == "olympiad" else "Обычный"

    text = (
        f"✅ <b>Название теста успешно обновлено!</b>\n\n"
        f"📋 <b>Управление тестом:</b>\n\n"
        f"📌 <b>Название:</b> {html.escape(new_title)}\n"
        f"🏷 <b>Тип:</b> {html.escape(type_str)}\n"
        f"📊 <b>Вопросов:</b> {len(questions)}\n"
        f"👥 <b>Сдали учеников:</b> {len(submissions)}"
    )

    await message.answer(
        text,
        reply_markup=get_admin_test_actions_kb(test_id),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("delete_test:"))
async def delete_test_handler(callback: types.CallbackQuery):
    try:
        await callback.answer("Тест удален!")
    except TelegramBadRequest:
        pass
    try:
        test_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        return
    await delete_test(test_id)
    await list_admin_tests(callback)


# --- 5. Меню "Результаты" ---
@router.message(Command("results"))
@router.message(F.text.in_({"📊 Результаты", "📊 Результаты учеников"}))
async def show_results_overview(message: types.Message):
    tests = await get_tests(only_active=False)
    if not tests:
        await message.answer(
            "Пока нет созданных тестов. Нажмите <b>«📝 Создать тест»</b>, чтобы добавить первый!",
            parse_mode="HTML"
        )
        return

    teacher_id = message.from_user.id
    buttons = []
    if WEBAPP_URL:
        teacher_url = f"{WEBAPP_URL}/teacher?teacher_id={teacher_id}"
        buttons.append([
            InlineKeyboardButton(text="🚀 Открыть ведомость (Mini App)", web_app=WebAppInfo(url=teacher_url))
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = (
        "📊 <b>Кабинет учителя: Результаты учеников</b>\n\n"
        "Откройте веб-ведомость прямо внутри Telegram, чтобы посмотреть ответы учеников и утвердить их оценки в 1 клик:"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# --- 6. Выдача сертификатов ученикам ---

@router.message(Command("certificate"))
@router.message(F.text == "🎓 Выдать сертификат")
@router.callback_query(F.data == "nav_issue_cert")
async def start_certificate_flow(event: types.Message | types.CallbackQuery, state: FSMContext):
    if isinstance(event, types.CallbackQuery):
        try:
            await event.answer()
        except TelegramBadRequest:
            pass
        msg = event.message
    else:
        msg = event
    await state.clear()

    classes = await get_distinct_student_classes()
    if not classes:
        text = (
            "👥 <b>Нет зарегистрированных учеников</b>\n\n"
            "В базе данных пока нет ни одного зарегистрированного ученика с указанным классом."
        )
        if isinstance(event, types.CallbackQuery):
            try:
                await msg.edit_text(text, parse_mode="HTML")
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise
        else:
            await msg.answer(text, parse_mode="HTML")
        return

    text = (
        "🎓 <b>Выдача сертификата / диплома</b>\n\n"
        "👥 <b>Выберите класс:</b>"
    )
    kb = get_cert_classes_kb(classes)
    if isinstance(event, types.CallbackQuery):
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    else:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.set_state(CertificateStates.selecting_class)


@router.callback_query(F.data == "cert_back_classes")
async def back_to_classes_callback(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    await state.clear()
    classes = await get_distinct_student_classes()
    if not classes:
        try:
            await callback.message.edit_text(
                "👥 <b>Нет зарегистрированных учеников</b>\n\n"
                "В базе данных пока нет ни одного зарегистрированного ученика с указанным классом.",
                parse_mode="HTML"
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        return

    text = (
        "🎓 <b>Выдача сертификата / диплома</b>\n\n"
        "👥 <b>Выберите класс:</b>"
    )
    kb = get_cert_classes_kb(classes)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await state.set_state(CertificateStates.selecting_class)


@router.callback_query(F.data.startswith("cert_cls:"))
async def process_class_selected(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    class_name = callback.data.split(":", 1)[1]
    await state.update_data(selected_class=class_name)
    students = await get_students_by_class(class_name)

    if not students:
        text = (
            f"🏫 Класс: <b>{html.escape(class_name)}</b>\n\n"
            "В этом классе пока нет зарегистрированных учеников."
        )
        try:
            await callback.message.edit_text(text, reply_markup=get_cert_empty_class_kb(), parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        await state.set_state(CertificateStates.selecting_student)
        return

    text = (
        f"🏫 Класс: <b>{html.escape(class_name)}</b>\n\n"
        "👤 <b>Выберите ученика:</b>"
    )
    kb = get_cert_students_kb(class_name, students)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await state.set_state(CertificateStates.selecting_student)


@router.callback_query(F.data.startswith("cert_search:"))
async def process_search_student_start(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    class_name = callback.data.split(":", 1)[1]
    await state.update_data(
        selected_class=class_name,
        cert_search_prompt_msg_id=callback.message.message_id
    )
    text = (
        f"🔍 <b>Поиск ученика в классе {html.escape(class_name)}</b>\n\n"
        "Введите фамилию или имя ученика сообщением в чат:\n"
        "<i>(например: Иванов или Иван)</i>"
    )
    try:
        await callback.message.edit_text(text, reply_markup=get_cert_search_prompt_kb(class_name), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await state.set_state(CertificateStates.searching_student)


@router.message(CertificateStates.searching_student, F.text, ~F.text.startswith("/"))
async def process_student_search_query(message: types.Message, state: FSMContext, bot: Optional[Bot] = None):
    query = message.text.strip()
    data = await state.get_data()
    selected_class = data.get("selected_class", "")
    prompt_msg_id = data.get("cert_search_prompt_msg_id")

    if not selected_class:
        await message.answer("❌ Ошибка: класс не выбран. Начните заново с помощью кнопки «🎓 Выдать сертификат».")
        await state.clear()
        return

    students = await get_students_by_class(selected_class, query=query)

    # Delete teacher's query message to keep chat spotless
    try:
        await message.delete()
    except Exception:
        pass

    if not students:
        text = (
            f"🔍 В классе <b>{html.escape(selected_class)}</b> по запросу «<b>{html.escape(query)}</b>» ничего не найдено.\n\n"
            "Попробуйте другой запрос или вернитесь к общему списку:"
        )
        kb = get_cert_no_results_kb(selected_class)
    else:
        text = (
            f"🔍 <b>Результаты поиска в классе {html.escape(selected_class)}:</b> «{html.escape(query)}»\n\n"
            "👤 <b>Выберите ученика:</b>"
        )
        kb = get_cert_students_kb(selected_class, students, is_search_result=True)

    # Edit the existing search prompt message if possible to avoid creating new messages
    effective_bot = bot or getattr(message, "bot", None)
    edited = False
    if prompt_msg_id and effective_bot:
        try:
            await effective_bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=prompt_msg_id,
                text=text,
                reply_markup=kb,
                parse_mode="HTML"
            )
            edited = True
        except Exception:
            edited = False

    if not edited:
        sent_msg = await message.answer(text, reply_markup=kb, parse_mode="HTML")
        await state.update_data(cert_search_prompt_msg_id=sent_msg.message_id)

    if students:
        await state.set_state(CertificateStates.selecting_student)


@router.callback_query(F.data.startswith("cert_stu:"))
async def process_student_selected(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    try:
        student_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return

    student = await get_user_by_id(student_id)
    if not student:
        try:
            await callback.message.answer("⚠️ Ученик не найден в базе данных.", parse_mode="HTML")
        except TelegramBadRequest:
            pass
        return

    full_name = student.get("full_name") or "Ученик"
    class_name = student.get("class_name") or ""
    student_tg_id = student.get("telegram_id")

    prompt_text = (
        f"👤 Выбран ученик: <b>{html.escape(full_name)} ({html.escape(class_name)})</b>\n\n"
        "Отправьте файл сертификата (PDF, фото или документ):"
    )
    cancel_kb = get_cert_cancel_kb()

    prompt_msg_id = None
    try:
        await callback.message.delete()
        new_msg = await callback.message.answer(prompt_text, reply_markup=cancel_kb, parse_mode="HTML")
        prompt_msg_id = getattr(new_msg, "message_id", None)
    except Exception:
        new_msg = await callback.message.edit_text(prompt_text, reply_markup=cancel_kb, parse_mode="HTML")
        prompt_msg_id = getattr(new_msg, "message_id", getattr(callback.message, "message_id", None))

    await state.update_data(
        student_id=student["id"],
        student_tg_id=student_tg_id,
        full_name=full_name,
        class_name=class_name,
        cert_prompt_msg_id=prompt_msg_id
    )
    await state.set_state(CertificateStates.waiting_for_certificate)
    await callback.answer()


@router.message(CertificateStates.waiting_for_certificate, F.photo | F.document | F.video | F.animation)
async def process_certificate_upload(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    student_tg_id = data.get("student_tg_id")
    full_name = data.get("full_name") or "Ученик"
    class_name = data.get("class_name") or ""
    prompt_msg_id = data.get("cert_prompt_msg_id")

    admin_kb = get_admin_main_kb(teacher_id=message.from_user.id)

    if not student_tg_id:
        await message.answer(
            f"❌ <b>Не удалось доставить сертификат:</b> у ученика {html.escape(full_name)} отсутствует Telegram ID.",
            parse_mode="HTML",
            reply_markup=admin_kb
        )
        await state.clear()
        return

    student_caption = (
        f"🏆 <b>Поздравляем, {html.escape(full_name)}!</b>\n\n"
        "Учитель наградил вас сертификатом/дипломом!\n"
        "Файл прикреплен ниже ⬇️"
    )

    intro_msg = None
    try:
        intro_msg = await bot.send_message(
            chat_id=student_tg_id,
            text=student_caption,
            parse_mode="HTML"
        )
        if message.document:
            await bot.send_document(
                chat_id=student_tg_id,
                document=message.document.file_id,
                caption=message.caption,
                caption_entities=message.caption_entities
            )
        elif message.photo:
            await bot.send_photo(
                chat_id=student_tg_id,
                photo=message.photo[-1].file_id,
                caption=message.caption,
                caption_entities=message.caption_entities
            )
        elif message.video:
            await bot.send_video(
                chat_id=student_tg_id,
                video=message.video.file_id,
                caption=message.caption,
                caption_entities=message.caption_entities
            )
        elif message.animation:
            await bot.send_animation(
                chat_id=student_tg_id,
                animation=message.animation.file_id,
                caption=message.caption,
                caption_entities=message.caption_entities
            )
    except TelegramForbiddenError:
        if intro_msg and getattr(intro_msg, "message_id", None):
            try:
                await bot.delete_message(chat_id=student_tg_id, message_id=intro_msg.message_id)
            except Exception:
                pass
        logger.warning("Student %s (%s) has blocked the bot.", full_name, student_tg_id)
        await message.answer(
            f"❌ <b>Не удалось доставить сертификат ученику {html.escape(full_name)} ({html.escape(class_name)}).</b>\n\n"
            "Причина: ученик заблокировал бота.",
            parse_mode="HTML",
            reply_markup=admin_kb
        )
        await state.clear()
        return
    except TelegramBadRequest as e:
        if intro_msg and getattr(intro_msg, "message_id", None):
            try:
                await bot.delete_message(chat_id=student_tg_id, message_id=intro_msg.message_id)
            except Exception:
                pass
        logger.warning("TelegramBadRequest sending cert to %s (%s): %s", full_name, student_tg_id, e)
        await message.answer(
            f"❌ <b>Не удалось доставить сертификат ученику {html.escape(full_name)} ({html.escape(class_name)}).</b>\n\n"
            "Причина: чат с учеником не найден или аккаунт удален.",
            parse_mode="HTML",
            reply_markup=admin_kb
        )
        await state.clear()
        return
    except Exception as e:
        if intro_msg and getattr(intro_msg, "message_id", None):
            try:
                await bot.delete_message(chat_id=student_tg_id, message_id=intro_msg.message_id)
            except Exception:
                pass
        logger.exception("Failed to deliver certificate to student %s: %s", student_tg_id, e)
        await message.answer(
            f"❌ <b>Не удалось доставить сертификат ученику {html.escape(full_name)} ({html.escape(class_name)}):</b>\n"
            f"<code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=admin_kb
        )
        await state.clear()
        return

    # Remove the cancel button from the prompt message to prevent accidental cancellation after delivery
    if prompt_msg_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=prompt_msg_id,
                reply_markup=None
            )
        except Exception:
            pass

    await message.answer(
        f"✅ <b>Сертификат успешно доставлен ученику {html.escape(full_name)} ({html.escape(class_name)})!</b>",
        parse_mode="HTML",
        reply_markup=admin_kb
    )
    await state.clear()


@router.message(CertificateStates.waiting_for_certificate, ~F.text.startswith("/"))
async def process_certificate_wrong_content(message: types.Message):
    await message.answer(
        "⚠️ Пожалуйста, отправьте файл сертификата (<b>PDF</b>, <b>фото</b> или <b>документ</b>).\n\n"
        "Если хотите отменить выдачу, нажмите кнопку <b>«❌ Отмена»</b> ниже.",
        reply_markup=get_cert_cancel_kb(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_open_main_menu")
async def admin_open_main_menu_callback(callback: types.CallbackQuery):
    try:
        await callback.answer("Добро пожаловать в English Bot 2.0! 🎉")
    except Exception:
        pass
    kb = get_admin_main_kb(teacher_id=callback.from_user.id)
    await callback.message.answer(
        "👋 <b>Главное меню учителя</b>\n\nВыберите нужное действие в меню ниже:",
        reply_markup=kb,
        parse_mode="HTML"
    )
