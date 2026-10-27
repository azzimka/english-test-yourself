import html
import logging
import asyncio
from io import BytesIO
from aiogram import Router, types, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMIN_IDS, WEBAPP_URL
from database import (
    create_test_with_questions,
    get_tests,
    get_test_by_id,
    toggle_test_active,
    delete_test,
    get_test_questions,
    get_test_submissions,
)
from services import (
    extract_text_from_file,
    UnsupportedFileError,
    FileExtractionError,
    parse_test_content,
    ParsedTest,
    generate_results_excel,
)
from keyboards.admin_kb import (
    get_admin_main_kb,
    get_test_type_choice_kb,
    get_title_choice_kb,
    get_confirm_test_kb,
    get_admin_tests_kb,
    get_admin_test_actions_kb,
)
from states.admin_states import UploadTestStates

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
@router.message(F.text == "📝 Создать тест")
@router.callback_query(F.data == "nav_create_test")
async def start_create_test(event: types.Message | types.CallbackQuery, state: FSMContext):
    await state.clear()
    msg = event if isinstance(event, types.Message) else event.message
    if isinstance(event, types.CallbackQuery):
        await event.answer()

    await msg.answer(
        "📝 <b>Создание нового теста</b>\n\n"
        "Выберите категорию теста:",
        reply_markup=get_test_type_choice_kb(),
        parse_mode="HTML"
    )
    await state.set_state(UploadTestStates.waiting_for_test_type)


@router.callback_query(F.data.startswith("set_test_type:"))
async def process_test_type_choice(callback: types.CallbackQuery, state: FSMContext):
    test_type = callback.data.split(":")[1]
    await state.update_data(test_type=test_type)
    
    type_name = "Олимпиадный тест" if test_type == "olympiad" else "Обычный тест"
    await callback.message.edit_text(
        f"Выбрана категория: <b>{type_name}</b>\n\n"
        "✍️ <b>Как назовем этот тест?</b>\n"
        "Напишите название сообщением (например: <i>Unit 4: Conditionals</i> или <i>Школьная олимпиада 10 класс</i>):\n\n"
        "<i>Или нажмите кнопку ниже, чтобы ИИ определил название сам из материалов файла.</i>",
        reply_markup=get_title_choice_kb(),
        parse_mode="HTML"
    )
    await state.set_state(UploadTestStates.waiting_for_title)
    await callback.answer()


@router.callback_query(F.data == "skip_title", UploadTestStates.waiting_for_title)
async def process_skip_title(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(custom_title=None)
    await ask_for_test_content(callback.message, state, is_edit=True)
    await callback.answer()


@router.message(UploadTestStates.waiting_for_title, F.text)
async def process_custom_title(message: types.Message, state: FSMContext):
    title = message.text.strip()
    await state.update_data(custom_title=title)
    await ask_for_test_content(message, state, is_edit=False)


async def ask_for_test_content(message: types.Message, state: FSMContext, is_edit: bool = False):
    data = await state.get_data()
    title = data.get("custom_title")
    title_str = f"«{html.escape(title)}»" if title else "<i>(определит ИИ)</i>"

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


@router.message(UploadTestStates.waiting_for_content, F.text | F.document)
async def process_test_upload(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    test_type = data.get("test_type", "regular")

    text_content = None
    if message.text:
        text_content = message.text.strip()
        if len(text_content) < 10:
            await message.answer(
                "❌ Текст задания слишком короткий. Пожалуйста, отправьте полные материалы теста или документ.",
                parse_mode="HTML"
            )
            return
    elif message.document:
        file_name = message.document.file_name or ""
        file_io = BytesIO()
        await bot.download(message.document, destination=file_io)
        file_bytes = file_io.getvalue()

        try:
            text_content = extract_text_from_file(file_name, file_bytes)
        except (UnsupportedFileError, FileExtractionError) as file_err:
            await message.answer(str(file_err), parse_mode="HTML")
            return

    # Запускаем живую динамическую анимацию статуса
    status_msg = await message.answer(
        LOADING_STEPS[0],
        parse_mode="HTML"
    )
    stop_event = asyncio.Event()
    anim_task = asyncio.create_task(animate_loading_status(status_msg, bot, stop_event))

    try:
        # Запрос к нейросети
        parsed_test: ParsedTest = await parse_test_content(
            text_content=text_content,
            test_type=test_type
        )
    except Exception as e:
        logger.exception("Error processing uploaded test")
        stop_event.set()
        try:
            await anim_task
        except Exception:
            pass
        await status_msg.edit_text(
            f"❌ <b>Произошла ошибка при обработке теста:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode="HTML"
        )
        return
    finally:
        stop_event.set()
        try:
            await anim_task
        except Exception:
            pass

    if not parsed_test or not parsed_test.questions:
        await status_msg.edit_text(
            "⚠️ Не удалось обнаружить вопросы в предоставленных материалах. Проверьте файл и попробуйте снова.",
            parse_mode="HTML"
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
    preview_lines = [
        "✅ <b>Тест успешно распознан!</b>\n",
        f"📌 <b>Название:</b> {html.escape(parsed_dict['title'])}",
        f"📊 <b>Всего вопросов:</b> {len(parsed_dict['questions'])}",
        f"🏷 <b>Категория:</b> {type_name}\n",
        "Опубликовать этот тест для учеников?"
    ]

    await message.edit_text("\n".join(preview_lines), reply_markup=get_confirm_test_kb(), parse_mode="HTML")
    await state.set_state(UploadTestStates.waiting_for_confirmation)


# --- 2.3 Обработка случайной отправки файла вне сценария создания теста ---
@router.message(F.document)
async def handle_unexpected_file_upload(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == UploadTestStates.waiting_for_content:
        return

    await message.answer(
        "📄 <b>Я вижу ваш документ!</b>\n\n"
        "Чтобы загрузить его в базу, нажмите кнопку <b>«📝 Создать тест»</b> в меню внизу, "
        "выберите категорию и отправьте файл повторно.",
        parse_mode="HTML"
    )


@router.message(F.photo)
async def handle_unexpected_photo_upload(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == UploadTestStates.waiting_for_content:
        return

    await message.answer(
        "📷 <b>Анализ фотографий не поддерживается</b>\n\n"
        "Бот работает с электронными документами <b>PDF (.pdf)</b>, <b>Word (.docx)</b> или текстом сообщений.",
        parse_mode="HTML"
    )


@router.callback_query(F.data == "edit_title_btn", UploadTestStates.waiting_for_confirmation)
async def start_editing_title(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.reply("✍️ Напишите новое название для этого теста сообщением:")
    await state.set_state(UploadTestStates.editing_title)
    await callback.answer()


@router.message(UploadTestStates.editing_title, F.text)
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
    data = await state.get_data()
    parsed_dict = data.get("parsed_test")
    test_type = data.get("test_type", "regular")

    if not parsed_dict:
        await callback.message.edit_text("❌ Данные теста устарели. Пожалуйста, начните заново.")
        await state.clear()
        return

    test_id = await create_test_with_questions(
        title=parsed_dict.get("title", "Тест по английскому"),
        test_type=test_type,
        created_by=callback.from_user.id,
        questions=parsed_dict.get("questions", []),
        description=parsed_dict.get("description", "")
    )
    await state.clear()

    type_name = "Олимпиадные тесты" if test_type == "olympiad" else "Обычные тесты"
    await callback.message.edit_text(
        f"🎉 <b>Тест опубликован!</b> (ID: {test_id})\n\n"
        f"Ученики уже могут найти его в разделе <b>«{html.escape(type_name)}»</b> и приступить к решению.",
        parse_mode="HTML"
    )
    await callback.answer("Опубликовано!")


# --- 4. Меню "Мои тесты" ---
@router.message(F.text == "📂 Мои тесты")
@router.callback_query(F.data == "admin_tests_list")
async def list_admin_tests(event: types.Message | types.CallbackQuery):
    tests = await get_tests(only_active=False)
    if not tests:
        text = "У вас пока нет созданных тестов. Нажмите <b>«📝 Создать тест»</b>, чтобы добавить первый!"
        if isinstance(event, types.CallbackQuery):
            await event.message.edit_text(text, parse_mode="HTML")
            await event.answer()
        else:
            await event.answer(text, parse_mode="HTML")
        return

    text = "📂 <b>Список ваших тестов:</b>\n🟢 — активен и виден ученикам\n🔴 — скрыт\n\nНажмите на тест для управления:"
    kb = get_admin_tests_kb(tests)
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("manage_test:"))
async def manage_single_test(callback: types.CallbackQuery):
    test_id = int(callback.data.split(":")[1])
    test = await get_test_by_id(test_id)
    if not test:
        await callback.answer("Тест не найден", show_alert=True)
        return

    questions = await get_test_questions(test_id)
    submissions = await get_test_submissions(test_id)

    status_str = "🟢 Активен (виден ученикам)" if test["is_active"] else "🔴 Скрыт от учеников"
    type_str = "Олимпиадный" if test["test_type"] == "olympiad" else "Обычный"

    text = (
        f"📋 <b>Управление тестом:</b>\n\n"
        f"📌 <b>Название:</b> {html.escape(test['title'])}\n"
        f"🏷 <b>Тип:</b> {html.escape(type_str)}\n"
        f"📊 <b>Вопросов:</b> {len(questions)}\n"
        f"👥 <b>Сдали учеников:</b> {len(submissions)}\n"
        f"Статус: {status_str}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=get_admin_test_actions_kb(test_id, bool(test["is_active"])),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("toggle_active:"))
async def toggle_active_handler(callback: types.CallbackQuery):
    test_id = int(callback.data.split(":")[1])
    await toggle_test_active(test_id)
    await manage_single_test(callback)


@router.callback_query(F.data.startswith("delete_test:"))
async def delete_test_handler(callback: types.CallbackQuery):
    test_id = int(callback.data.split(":")[1])
    await delete_test(test_id)
    await callback.answer("Тест удален!")
    await list_admin_tests(callback)


# --- 5. Меню "Результаты учеников" и выгрузка Excel ---
@router.message(F.text == "📊 Результаты учеников")
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

    buttons.append([
        InlineKeyboardButton(text="📥 Скачать Excel ведомость", callback_data="admin_export_menu")
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = (
        "📊 <b>Кабинет учителя: Результаты учеников</b>\n\n"
        "Откройте веб-ведомость прямо внутри Telegram, чтобы посмотреть ответы учеников и утвердить их оценки в 1 клик:"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_export_menu")
async def show_export_menu(callback: types.CallbackQuery):
    tests = await get_tests(only_active=False)
    text = "📊 <b>Выберите тест для скачивания Excel ведомости:</b>"
    await callback.message.edit_text(text, reply_markup=get_admin_tests_kb(tests), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("export_test_excel:"))
async def export_excel_handler(callback: types.CallbackQuery):
    test_id = int(callback.data.split(":")[1])
    test = await get_test_by_id(test_id)
    if not test:
        await callback.answer("Тест не найден")
        return

    submissions = await get_test_submissions(test_id)
    if not submissions:
        await callback.answer("Этот тест пока еще никто не сдал!", show_alert=True)
        return

    excel_io = generate_results_excel(test["title"], submissions)
    file_bytes = excel_io.getvalue()

    filename = f"Results_{test['title'][:20].replace(' ', '_')}.xlsx"
    input_file = BufferedInputFile(file_bytes, filename=filename)

    await callback.message.answer_document(
        document=input_file,
        caption=f"📊 Ведомость результатов по тесту:\n<b>{html.escape(test['title'])}</b>\nВсего сдач: {len(submissions)}",
        parse_mode="HTML"
    )
    await callback.answer()
