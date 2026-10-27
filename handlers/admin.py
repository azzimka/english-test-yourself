import html
import logging
from io import BytesIO
from aiogram import Router, types, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile

from config import ADMIN_IDS
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
    extract_text_from_docx,
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


# --- 1. Меню "Создать тест" ---
@router.message(F.text == "📝 Создать тест")
async def start_create_test(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "📝 **Создание нового теста**\n\n"
        "Выберите категорию теста:",
        reply_markup=get_test_type_choice_kb(),
        parse_mode="Markdown"
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
    title_str = f"«{title}»" if title else "<i>(определит ИИ)</i>"

    text = (
        f"📌 Название теста: <b>{title_str}</b>\n\n"
        "📥 <b>Теперь отправьте материалы теста:</b>\n"
        "• Документ <b>PDF</b> или <b>Word (.docx)</b>\n"
        "• Обычный текст сообщением\n"
        "• Или четкую фотографию / скан страницы\n\n"
        "ИИ автоматически распознает вопросы, варианты и правильные ответы."
    )
    if is_edit:
        await message.edit_text(text, parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")
    await state.set_state(UploadTestStates.waiting_for_content)


# --- 2. Обработка загружаемого контента (текст, документ Word, фото) ---
@router.message(UploadTestStates.waiting_for_content)
async def process_test_upload(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    test_type = data.get("test_type", "regular")
    status_msg = await message.answer(
        "⏳ **Нейросеть (Gemini 3.5 Flash) анализирует материалы...**\nОбычно это занимает 2–5 секунд.",
        parse_mode="Markdown"
    )

    text_content = None
    file_bytes = None
    mime_type = None

    try:
        if message.text:
            text_content = message.text
        elif message.document:
            file_name = (message.document.file_name or "").lower()
            file_io = BytesIO()
            await bot.download(message.document, destination=file_io)
            file_bytes = file_io.getvalue()

            if file_name.endswith(".docx"):
                mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            elif file_name.endswith(".pdf"):
                mime_type = "application/pdf"
            elif file_name.endswith((".txt", ".rtf")):
                mime_type = "text/plain"
            else:
                await status_msg.edit_text(
                    "❌ Пожалуйста, отправьте документ в формате **PDF**, **Word (.docx)**, текстовый файл или фото.",
                    parse_mode="Markdown"
                )
                return

        elif message.photo:
            photo = message.photo[-1]
            file_io = BytesIO()
            await bot.download(photo, destination=file_io)
            file_bytes = file_io.getvalue()
            mime_type = "image/jpeg"
        else:
            await status_msg.edit_text(
                "❌ Неподдерживаемый тип файла. Отправьте файл PDF, Word (.docx), текст или фото.",
                parse_mode="Markdown"
            )
            return

        # Запрос к Gemini
        parsed_test: ParsedTest = await parse_test_content(
            text_content=text_content,
            file_bytes=file_bytes,
            mime_type=mime_type,
            test_type=test_type
        )

        if not parsed_test.questions:
            await status_msg.edit_text("⚠️ Не удалось обнаружить вопросы в предоставленных материалах. Проверьте файл и попробуйте снова.")
            return

        # Если учитель заранее указал название, используем его
        custom_title = data.get("custom_title")
        if custom_title:
            parsed_test.title = custom_title

        # Сохраняем в FSM для подтверждения
        await state.update_data(parsed_test=parsed_test.model_dump())
        await render_test_preview(status_msg, parsed_test.model_dump(), test_type, state)

    except Exception as e:
        logger.exception("Error processing uploaded test")
        await status_msg.edit_text(f"❌ Произошла ошибка при обработке теста: {e}")


async def render_test_preview(message: types.Message, parsed_dict: dict, test_type: str, state: FSMContext):
    preview_lines = [
        "✅ <b>Тест успешно распознан!</b>\n",
        f"📌 <b>Название:</b> {html.escape(parsed_dict['title'])}",
        f"📊 <b>Всего вопросов:</b> {len(parsed_dict['questions'])}",
        f"🏷 <b>Категория:</b> {'Олимпиадный' if test_type == 'olympiad' else 'Обычный'}\n",
        "<b>Примеры первых вопросов:</b>"
    ]

    for i, q in enumerate(parsed_dict["questions"][:3], start=1):
        opts = ", ".join(q["options"]) if q.get("options") else "Ввод слова"
        preview_lines.append(f"\n<b>{i}. {html.escape(q['question_text'])}</b>")
        if q.get("options"):
            preview_lines.append(f"   Варианты: {html.escape(opts)}")
        preview_lines.append(f"   👉 Ответ: <code>{html.escape(str(q['correct_answer']))}</code>")

    if len(parsed_dict["questions"]) > 3:
        preview_lines.append(f"\n<i>...и еще {len(parsed_dict['questions']) - 3} вопросов.</i>")

    preview_lines.append("\nОпубликовать этот тест для учеников?")

    await message.edit_text("\n".join(preview_lines), reply_markup=get_confirm_test_kb(), parse_mode="HTML")
    await state.set_state(UploadTestStates.waiting_for_confirmation)


# --- 2.3 Обработка случайной отправки файла вне сценария создания теста ---
@router.message(F.document | F.photo)
async def handle_unexpected_file_upload(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == UploadTestStates.waiting_for_content:
        return

    await message.answer(
        "📄 <b>Я вижу ваш файл!</b>\n\n"
        "Чтобы загрузить его в базу, нажмите кнопку <b>«📝 Создать тест»</b> в меню внизу, "
        "выберите категорию и отправьте файл повторно.",
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
        f"🎉 **Тест опубликован!** (ID: {test_id})\n\n"
        f"Ученики уже могут найти его в разделе **«{type_name}»** и приступить к решению.",
        parse_mode="Markdown"
    )
    await callback.answer("Опубликовано!")


# --- 4. Меню "Мои тесты" ---
@router.message(F.text == "📂 Мои тесты")
@router.callback_query(F.data == "admin_tests_list")
async def list_admin_tests(event: types.Message | types.CallbackQuery):
    tests = await get_tests(only_active=False)
    if not tests:
        text = "У вас пока нет созданных тестов. Нажмите **«📝 Создать тест»**, чтобы добавить первый!"
        if isinstance(event, types.CallbackQuery):
            await event.message.edit_text(text, parse_mode="Markdown")
            await event.answer()
        else:
            await event.answer(text, parse_mode="Markdown")
        return

    text = "📂 **Список ваших тестов:**\n🟢 — активен и виден ученикам\n🔴 — скрыт\n\nНажмите на тест для управления:"
    kb = get_admin_tests_kb(tests)
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="Markdown")


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
        f"📋 **Управление тестом:**\n\n"
        f"📌 **Название:** {test['title']}\n"
        f"🏷 **Тип:** {type_str}\n"
        f"📊 **Вопросов:** {len(questions)}\n"
        f"👥 **Сдали учеников:** {len(submissions)}\n"
        f"Статус: {status_str}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=get_admin_test_actions_kb(test_id, bool(test["is_active"])),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("toggle_active:"))
async def toggle_active_handler(callback: types.CallbackQuery):
    test_id = int(callback.data.split(":")[1])
    new_status = await toggle_test_active(test_id)
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
        await message.answer("Пока нет тестов для отображения результатов.")
        return

    text = "📊 **Выберите тест для скачивания ведомости оценок:**"
    await message.answer(text, reply_markup=get_admin_tests_kb(tests), parse_mode="Markdown")


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
        caption=f"📊 Ведомость результатов по тесту:\n**{test['title']}**\nВсего сдач: {len(submissions)}"
    )
    await callback.answer()
