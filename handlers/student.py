import time
import html
import logging
from aiogram import Router, types, F, Bot
from aiogram.fsm.context import FSMContext

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
async def show_tests_catalog(message: types.Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала пройдите регистрацию: напишите команду /start")
        return

    test_type = "olympiad" if "Олимпиадные" in message.text else "regular"
    tests = await get_tests(test_type=test_type, only_active=True)

    if not tests:
        type_word = "олимпиадных" if test_type == "olympiad" else "обычных"
        await message.answer(f"Пока нет активных {type_word} тестов. Учительница добавит их в ближайшее время!")
        return

    # Получаем уже сданные тесты этого ученика
    submissions = await get_student_submissions(user["id"])
    completed_map = {s["test_id"]: s for s in submissions}

    header = "🏆 <b>Олимпиадные тесты:</b>" if test_type == "olympiad" else "📚 <b>Обычные тесты:</b>"
    text = f"{header}\nВыберите тест, который задала учительница:"
    kb = get_student_tests_kb(tests, completed_map)

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# --- 2. Карточка выбранного теста ---
@router.callback_query(F.data.startswith("select_test:"))
async def view_test_card(callback: types.CallbackQuery):
    test_id = int(callback.data.split(":")[1])
    test = await get_test_by_id(test_id)
    if not test or not test["is_active"]:
        await callback.answer("Этот тест сейчас недоступен", show_alert=True)
        return

    user = await get_user_by_telegram_id(callback.from_user.id)
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
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "back_to_catalog")
async def back_to_catalog_handler(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.message.answer("Выберите нужный раздел в меню ниже:", reply_markup=get_student_main_kb())
    await callback.answer()


# --- 3. Запуск и прохождение теста ---
@router.callback_query(F.data.startswith("begin_quiz:"))
async def start_quiz_handler(callback: types.CallbackQuery, state: FSMContext):
    test_id = int(callback.data.split(":")[1])
    test = await get_test_by_id(test_id)
    questions = await get_test_questions(test_id)

    if not questions:
        await callback.answer("В этом тесте нет вопросов!", show_alert=True)
        return

    user = await get_user_by_telegram_id(callback.from_user.id)

    # Защита от повторного прохождения олимпиады
    if test["test_type"] == "olympiad":
        existing = await get_student_submission(user["id"], test_id)
        if existing:
            await callback.answer("Вы уже сдали эту олимпиаду!", show_alert=True)
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

    await callback.message.delete()
    await send_quiz_question(callback.message, state)
    await callback.answer()


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
    if options:
        kb = get_question_options_kb(options)
        await message.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    else:
        lines.append("\n✍️ <i>Напишите ваш ответ словом в чат:</i>")
        await message.answer("\n".join(lines), parse_mode="HTML")


# Ответ через инлайн-кнопку
@router.callback_query(QuizStates.solving, F.data.startswith("quiz_choice:"))
async def process_quiz_button_answer(callback: types.CallbackQuery, state: FSMContext):
    opt_idx = int(callback.data.split(":")[1])
    data = await state.get_data()
    q_idx = data["current_index"]
    q = data["questions"][q_idx]

    selected_option = q["options"][opt_idx]
    await handle_user_answer(callback.message, state, selected_option, is_callback=True)
    await callback.answer()


# Ответ текстовым вводом (для открытых вопросов)
@router.message(QuizStates.solving, F.text)
async def process_quiz_text_answer(message: types.Message, state: FSMContext):
    data = await state.get_data()
    q_idx = data["current_index"]
    q = data["questions"][q_idx]

    # Если у вопроса были кнопки, но ученик пишет текстом, всё равно принимаем текст
    await handle_user_answer(message, state, message.text, is_callback=False)


async def handle_user_answer(message: types.Message, state: FSMContext, user_ans_raw: str, is_callback: bool):
    data = await state.get_data()
    q_idx = data["current_index"]
    q = data["questions"][q_idx]
    score = data["score"]
    answers = data["answers"]

    correct_raw = q["correct_answer"]

    # Проверка правильности
    user_norm = normalize_answer(user_ans_raw)
    correct_norm = normalize_answer(correct_raw)

    is_correct = (user_norm == correct_norm)
    # Также проверим, если правильный ответ — это буква (например "B"), а ученик выбрал "B) apple"
    if not is_correct and len(correct_norm) == 1:
        if str(user_ans_raw).strip().lower().startswith(correct_norm):
            is_correct = True

    if is_correct:
        score += 1

    answers.append({
        "question": q["question_text"],
        "user_answer": user_ans_raw,
        "correct_answer": correct_raw,
        "is_correct": is_correct,
        "explanation": q.get("explanation")
    })

    await state.update_data(current_index=q_idx + 1, score=score, answers=answers)
    await send_quiz_question(message, state)


# --- 4. Завершение теста ---
async def finish_quiz(message: types.Message, state: FSMContext):
    data = await state.get_data()
    test_id = data["test_id"]
    test_title = data["test_title"]
    score = data["score"]
    total = len(data["questions"])
    answers = data["answers"]
    start_time = data["start_time"]
    time_spent = int(time.time() - start_time)

    user = await get_user_by_telegram_id(message.chat.id)
    student_id = user["id"]

    await save_submission(
        student_id=student_id,
        test_id=test_id,
        score=score,
        total_questions=total,
        answers=answers,
        time_spent_seconds=time_spent
    )
    await state.clear()

    pct = round((score / total) * 100) if total > 0 else 0
    minutes = time_spent // 60
    seconds = time_spent % 60

    # Ученику показываем нейтральное сообщение без оценок и ответов
    student_msg = (
        "🏁 <b>Работа успешно отправлена!</b>\n\n"
        f"📝 <b>Тест:</b> {html.escape(test_title)}\n"
        f"⏱ <b>Время решения:</b> {minutes} мин. {seconds} сек.\n\n"
        "✅ Ваши ответы сохранены и переданы учителю на проверку.\n"
        "Оценки и результаты будут объявлены учителем."
    )
    await message.answer(student_msg, reply_markup=get_student_main_kb(), parse_mode="HTML")

    # Уведомление для учителя (краткая карточка)
    bot: Bot = message.bot
    report_text = (
        "🔔 <b>Ученик сдал тест!</b>\n\n"
        f"👤 <b>Ученик:</b> {html.escape(user['full_name'])} ({html.escape(user['class_name'])})\n"
        f"📝 <b>Тест:</b> {html.escape(test_title)}\n"
        f"🎯 <b>Итоговый результат:</b> {score}/{total} ({pct}%)\n"
        f"⏱ <b>Время:</b> {minutes} мин. {seconds} сек."
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, report_text, parse_mode="HTML")
        except Exception as e:
            logger.warning("Failed to notify admin %s: %s", admin_id, e)


# --- 5. Мои результаты ---
@router.message(F.text == "📈 Мои результаты")
async def show_student_history(message: types.Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала пройдите регистрацию: напишите команду /start")
        return

    submissions = await get_student_submissions(user["id"])
    if not submissions:
        await message.answer("Вы еще не прошли ни одного теста. Выберите тест в меню и начните!")
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

    await message.answer("\n\n".join(lines), parse_mode="HTML")
