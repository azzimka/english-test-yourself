import time
import html
import logging
from aiogram import Router, types, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from config import ADMIN_IDS
from database import (
    get_user_by_telegram_id,
    get_tests,
    get_test_by_id,
    get_test_questions,
    save_submission,
    get_student_submission,
    get_student_submissions,
)
from keyboards.student_kb import (
    get_student_main_kb,
    get_student_tests_kb,
    get_start_test_confirm_kb,
    get_question_options_kb,
)
from states.student_states import QuizStates

logger = logging.getLogger(__name__)
router = Router()

def normalize_answer(text: str) -> str:
    """Нормализует ответ для сравнения (удаляет скобки, пробелы, переводит в нижний регистр)."""
    t = str(text).strip().lower()
    # Если ответ в виде "A) apple" или "A. apple", извлекаем и букву, и текст
    for prefix in ["a)", "b)", "c)", "d)", "e)", "a.", "b.", "c.", "d.", "e."]:
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
            break
    return t


# --- 1. Просмотр доступных тестов ---
@router.message(F.text.in_(["📚 Обычные тесты", "🏆 Олимпиадные тесты"]))
@router.callback_query(F.data.in_(["nav_student_regular", "nav_student_olympiad"]))
async def show_tests_catalog(event: types.Message | types.CallbackQuery):
    user_id = event.from_user.id
    msg = event if isinstance(event, types.Message) else event.message
    if isinstance(event, types.CallbackQuery):
        try:
            await event.answer()
        except TelegramBadRequest:
            pass

    user = await get_user_by_telegram_id(user_id)
    if not user:
        await msg.answer("Сначала пройдите регистрацию: напишите команду /start", parse_mode="HTML")
        return

    event_text = getattr(event, "text", "") or ""
    event_data = getattr(event, "data", "") or ""
    test_type = "olympiad" if ("Олимпиадные" in event_text or "olympiad" in event_data) else "regular"
    tests = await get_tests(test_type=test_type, only_active=True)

    if not tests:
        type_word = "олимпиадных" if test_type == "olympiad" else "обычных"
        await msg.answer(f"Пока нет активных {type_word} тестов. Учительница добавит их в ближайшее время!", parse_mode="HTML")
        return

    # Получаем уже сданные тесты этого ученика
    submissions = await get_student_submissions(user["id"])
    completed_map = {s["test_id"]: s for s in submissions}

    header = "🏆 <b>Олимпиадные тесты:</b>" if test_type == "olympiad" else "📚 <b>Обычные тесты:</b>"
    text = f"{header}\nВыберите тест, который задала учительница:"
    kb = get_student_tests_kb(tests, completed_map)

    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# --- 2. Карточка выбранного теста ---
@router.callback_query(F.data.startswith("select_test:"))
async def view_test_card(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    try:
        test_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        return

    if not callback.message:
        return

    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user:
        try:
            await callback.message.answer("Сначала пройдите регистрацию: напишите команду /start", parse_mode="HTML")
        except TelegramBadRequest:
            pass
        return

    test = await get_test_by_id(test_id)
    if not test or not test["is_active"]:
        try:
            await callback.message.answer("⚠️ Этот тест сейчас недоступен.", parse_mode="HTML")
        except TelegramBadRequest:
            pass
        return

    sub = await get_student_submission(user["id"], test_id)
    questions = await get_test_questions(test_id)

    is_olympiad = (test["test_type"] == "olympiad")
    already_passed = (sub is not None)

    info_lines = [
        f"📋 <b>Тест:</b> {html.escape(test['title'])}",
        f"🏷 <b>Категория:</b> {'Олимпиадный' if is_olympiad else 'Обычный'}",
        f"❓ <b>Количество вопросов:</b> {len(questions)}"
    ]

    if already_passed:
        info_lines.append("\n✅ <b>Вы уже отправили ответы по этому тесту.</b>\n<i>Результаты проверяются учителем.</i>")
        if is_olympiad:
            info_lines.append("⚠️ <i>Повторное прохождение олимпиады заблокировано.</i>")

    text = "\n".join(info_lines)
    kb = get_start_test_confirm_kb(test_id, already_passed, is_olympiad, student_tg_id=callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


@router.callback_query(F.data == "back_to_catalog")
async def back_to_catalog_handler(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer("Выберите нужный раздел в меню ниже:", reply_markup=get_student_main_kb(), parse_mode="HTML")


# --- 3. Запуск и прохождение теста ---
@router.callback_query(F.data.startswith("begin_quiz:"))
async def start_quiz_handler(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    try:
        test_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        return

    if not callback.message:
        return

    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user:
        try:
            await callback.message.answer("Сначала пройдите регистрацию: напишите команду /start", parse_mode="HTML")
        except TelegramBadRequest:
            pass
        return

    test = await get_test_by_id(test_id)
    if not test or not test["is_active"]:
        try:
            await callback.message.answer("⚠️ Этот тест сейчас недоступен.", parse_mode="HTML")
        except TelegramBadRequest:
            pass
        return

    questions = await get_test_questions(test_id)
    if not questions:
        try:
            await callback.message.answer("⚠️ В этом тесте нет вопросов!", parse_mode="HTML")
        except TelegramBadRequest:
            pass
        return

    # Защита от повторного прохождения олимпиады
    if test["test_type"] == "olympiad":
        existing = await get_student_submission(user["id"], test_id)
        if existing:
            try:
                await callback.message.answer("⚠️ Вы уже сдали эту олимпиаду!", parse_mode="HTML")
            except TelegramBadRequest:
                pass
            return

    await state.set_state(QuizStates.solving)
    await state.update_data(
        test_id=test_id,
        test_title=test["title"],
        test_type=test["test_type"],
        questions=questions,
        current_index=0,
        score=0,
        answers=[],
        start_time=time.time()
    )

    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_quiz_question(callback.message, state)


async def send_quiz_question(message: types.Message, state: FSMContext):
    data = await state.get_data()
    q_idx = data["current_index"]
    questions = data["questions"]
    total = len(questions)

    if q_idx >= total:
        await finish_quiz(message, state)
        return

    q = questions[q_idx]
    order_num = q_idx + 1

    lines = []
    # Если есть общий контекст / текст для чтения
    if q.get("context_text"):
        lines.append(f"📖 <b>Текст к заданию:</b>\n<i>{html.escape(q['context_text'])}</i>\n")

    lines.append(f"❓ <b>Вопрос {order_num} из {total}:</b>\n{html.escape(q['question_text'])}")

    options = q.get("options", [])
    if q.get("question_type") == "multiple_choice":
        lines.append("\n☑️ <b>Вопрос с несколькими вариантами ответов!</b>\n<i>Напишите буквы выбранных ответов через запятую в чат (например: A, C):</i>")
        if options:
            opts_text = "\n".join(options)
            lines.append(f"\n{html.escape(opts_text)}")
        await message.answer("\n".join(lines), parse_mode="HTML")
    elif options:
        kb = get_question_options_kb(options)
        await message.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    else:
        lines.append("\n✍️ <i>Напишите ваш ответ словом в чат:</i>")
        await message.answer("\n".join(lines), parse_mode="HTML")


# Ответ через инлайн-кнопку
@router.callback_query(QuizStates.solving, F.data.startswith("quiz_choice:"))
async def process_quiz_button_answer(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    data = await state.get_data()
    q_idx = data.get("current_index", 0)
    questions = data.get("questions", [])
    if q_idx >= len(questions):
        return
    q = questions[q_idx]

    try:
        opt_idx = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        return

    options = q.get("options", [])
    if opt_idx < 0 or opt_idx >= len(options):
        return

    selected_option = options[opt_idx]

    # Remove the reply markup to prevent re-answering or rapid double clicks
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower() and "message to edit not found" not in str(e).lower():
                pass
        except Exception:
            pass

    await handle_user_answer(callback.message, state, selected_option, is_callback=True)


# Ответ текстовым вводом (для открытых вопросов)
@router.message(QuizStates.solving, F.text, ~F.text.startswith("/"))
async def process_quiz_text_answer(message: types.Message, state: FSMContext):
    data = await state.get_data()
    q_idx = data.get("current_index", 0)
    questions = data.get("questions", [])
    if q_idx >= len(questions):
        return

    # Если у вопроса были кнопки, но ученик пишет текстом, всё равно принимаем текст
    await handle_user_answer(message, state, message.text, is_callback=False)


async def handle_user_answer(message: types.Message, state: FSMContext, user_ans_raw: str, is_callback: bool):
    data = await state.get_data()
    q_idx = data.get("current_index", 0)
    questions = data.get("questions", [])
    if q_idx >= len(questions):
        return

    q = questions[q_idx]
    answers = data.get("answers", [])

    correct_raw = q.get("correct_answer", "")

    # Сохраняем ответ ученика без автоматической оценки нейросетью (оценивает учитель)
    answers.append({
        "question": q.get("question_text", ""),
        "user_answer": user_ans_raw,
        "correct_answer": correct_raw,
        "is_correct": None,
        "explanation": q.get("explanation"),
        "image_url": q.get("image_url"),
        "question_type": q.get("question_type", "single_choice" if q.get("options") else "open_text")
    })

    await state.update_data(current_index=q_idx + 1, score=0, answers=answers)
    await send_quiz_question(message, state)


# --- 4. Завершение теста ---
async def finish_quiz(message: types.Message, state: FSMContext):
    data = await state.get_data()
    test_id = data.get("test_id")
    test_title = data.get("test_title", "Тест")
    questions = data.get("questions", [])
    total = len(questions)
    answers = data.get("answers", [])
    start_time = data.get("start_time", time.time())
    time_spent = int(time.time() - start_time)

    user = await get_user_by_telegram_id(message.chat.id)
    if not user:
        student_id = message.chat.id
        student_name = "Ученик"
        class_name = "-"
    else:
        student_id = user["id"]
        student_name = user.get("full_name", "Ученик")
        class_name = user.get("class_name", "-")

    if test_id:
        await save_submission(
            student_id=student_id,
            test_id=test_id,
            score=0,
            total_questions=total,
            answers=answers,
            time_spent_seconds=time_spent
        )
    await state.clear()

    minutes = time_spent // 60
    seconds = time_spent % 60

    # Ученику показываем нейтральное сообщение без оценок и ответов
    student_msg = (
        "🏁 <b>Работа успешно отправлена!</b>\n\n"
        f"📝 <b>Тест:</b> {html.escape(test_title)}\n"
        f"⏱ <b>Время решения:</b> {minutes} мин. {seconds} сек.\n\n"
        "✅ Ваши ответы отправлены на проверку!"
    )
    await message.answer(student_msg, reply_markup=get_student_main_kb(), parse_mode="HTML")

    # Уведомление для учителя (краткая карточка без предварительного ИИ скоринга)
    bot: Bot = getattr(message, "bot", None)
    if bot:
        report_text = (
            "🔔 <b>Ученик сдал тест!</b>\n\n"
            f"👤 <b>Ученик:</b> {html.escape(student_name)} ({html.escape(class_name)})\n"
            f"📝 <b>Тест:</b> {html.escape(test_title)}\n"
            f"⏱ <b>Время решения:</b> {minutes} мин. {seconds} сек.\n\n"
            "📋 <i>Работа ожидает вашей проверки в Кабинете учителя.</i>"
        )

        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, report_text, parse_mode="HTML")
            except Exception as e:
                logger.warning("Failed to notify admin %s: %s", admin_id, e)


# --- 5. Мои результаты ---
@router.message(F.text == "📈 Мои результаты")
@router.callback_query(F.data == "nav_student_results")
async def show_student_history(event: types.Message | types.CallbackQuery):
    user_id = event.from_user.id
    msg = event if isinstance(event, types.Message) else event.message
    if isinstance(event, types.CallbackQuery):
        try:
            await event.answer()
        except TelegramBadRequest:
            pass

    user = await get_user_by_telegram_id(user_id)
    if not user:
        await msg.answer("Сначала пройдите регистрацию: напишите команду /start", parse_mode="HTML")
        return

    submissions = await get_student_submissions(user["id"])
    if not submissions:
        await msg.answer("Вы еще не прошли ни одного теста. Выберите тест в меню и начните!", parse_mode="HTML")
        return

    lines = ["📈 <b>История ваших тестов:</b>\n"]
    for s in submissions[:20]:
        icon = "🏆" if s.get("test_type") == "olympiad" else "📚"
        status = s.get("status", "pending")

        if status == "confirmed":
            score = s["score"]
            total = s["total_questions"]
            pct = round((score / total) * 100) if total > 0 else 0
            lines.append(
                f"{icon} <b>{html.escape(s['test_title'])}</b>\n"
                f"   Результат: <b>{score}/{total} ({pct}%)</b>"
            )
        else:
            lines.append(
                f"{icon} <b>{html.escape(s['test_title'])}</b>\n"
                f"   Статус: <i>Сдано на проверку</i>"
            )

    await msg.answer("\n\n".join(lines), parse_mode="HTML")
