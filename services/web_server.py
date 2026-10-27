import html
import logging
from aiohttp import web
from aiogram import Bot

from config import WEB_DIR, ADMIN_IDS
from database import (
    get_user_by_telegram_id,
    get_test_by_id,
    get_test_questions,
    get_student_submission,
    save_submission,
)

logger = logging.getLogger(__name__)

def normalize_answer(text: str) -> str:
    t = str(text).strip().lower()
    for prefix in ["a)", "b)", "c)", "d)", "e)", "a.", "b.", "c.", "d.", "e."]:
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
            break
    return t


def create_web_app(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot

    # Static routes
    app.router.add_get("/", handle_index)
    app.router.add_static("/static/", path=WEB_DIR, name="static")

    # API routes
    app.router.add_get("/api/test", handle_api_get_test)
    app.router.add_post("/api/submit", handle_api_submit)

    return app


async def handle_index(request: web.Request) -> web.Response:
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        return web.Response(text="index.html not found", status=404)
    return web.FileResponse(index_file)


async def handle_api_get_test(request: web.Request) -> web.Response:
    test_id_str = request.query.get("test_id")
    student_tg_id_str = request.query.get("student_id")

    if not test_id_str or not student_tg_id_str:
        return web.json_response({"error": "Отсутствуют обязательные параметры."}, status=400)

    try:
        test_id = int(test_id_str)
        student_tg_id = int(student_tg_id_str)
    except ValueError:
        return web.json_response({"error": "Некорректный ID теста или ученика."}, status=400)

    test = await get_test_by_id(test_id)
    if not test or not test["is_active"]:
        return web.json_response({"error": "Тест не найден или скрыт учителем."})

    student = await get_user_by_telegram_id(student_tg_id)
    if not student:
        return web.json_response({"error": "Ученик не зарегистрирован. Отправьте /start в боте."})

    # Check if already submitted (for olympiads)
    existing_sub = await get_student_submission(student["id"], test_id)
    if existing_sub and test["test_type"] == "olympiad":
        return web.json_response({"error": "Вы уже сдали эту олимпиаду. Повторная сдача запрещена."})

    questions = await get_test_questions(test_id)

    # Security: strip correct_answer and explanation before sending to browser
    safe_questions = []
    for q in questions:
        safe_questions.append({
            "id": q["id"],
            "order_num": q["order_num"],
            "question_text": q["question_text"],
            "context_text": q.get("context_text"),
            "options": q.get("options", [])
        })

    return web.json_response({
        "test": {
            "id": test["id"],
            "title": test["title"],
            "test_type": test["test_type"],
            "questions": safe_questions
        },
        "student": {
            "id": student["id"],
            "telegram_id": student["telegram_id"],
            "full_name": student["full_name"],
            "class_name": student["class_name"]
        }
    })


async def handle_api_submit(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Невалидный JSON запрос."}, status=400)

    test_id = data.get("test_id")
    student_id = data.get("student_id")
    user_answers = data.get("answers", {}) # { "qid": "val" }
    time_spent = int(data.get("time_spent_seconds", 0))

    if not test_id or not student_id:
        return web.json_response({"error": "Не указан test_id или student_id."}, status=400)

    test = await get_test_by_id(test_id)
    if not test:
        return web.json_response({"error": "Тест не найден."}, status=404)

    questions = await get_test_questions(test_id)
    q_map = {q["id"]: q for q in questions}

    score = 0
    detailed_answers = []

    for q in questions:
        qid_str = str(q["id"])
        user_ans_raw = user_answers.get(qid_str, "").strip()
        correct_raw = q["correct_answer"].strip()

        user_norm = normalize_answer(user_ans_raw)
        correct_norm = normalize_answer(correct_raw)

        is_correct = (user_norm == correct_norm)
        if not is_correct and len(correct_norm) == 1 and user_ans_raw:
            if user_ans_raw.lower().startswith(correct_norm):
                is_correct = True

        if is_correct:
            score += 1

        detailed_answers.append({
            "question": q["question_text"],
            "user_answer": user_ans_raw if user_ans_raw else "(Без ответа)",
            "correct_answer": correct_raw,
            "is_correct": is_correct,
            "explanation": q.get("explanation")
        })

    total = len(questions)

    # Save to database
    sub_id = await save_submission(
        student_id=student_id,
        test_id=test_id,
        score=score,
        total_questions=total,
        answers=detailed_answers,
        time_spent_seconds=time_spent
    )

    # Get student info for notification
    # Find student by internal ID from submissions
    from database.db import DB_PATH
    import aiosqlite
    student_info = None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (student_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                student_info = dict(row)

    # Notify teacher via Telegram
    bot: Bot = request.app["bot"]
    pct = round((score / total) * 100) if total > 0 else 0
    minutes = time_spent // 60
    seconds = time_spent % 60

    st_name = student_info["full_name"] if student_info else "Ученик"
    st_class = student_info["class_name"] if student_info else ""

    breakdown_lines = [
        "🔔 <b>Ученик сдал тест (через Mini App)!</b>\n",
        f"👤 <b>Ученик:</b> {html.escape(st_name)} ({html.escape(st_class)})",
        f"📝 <b>Тест:</b> {html.escape(test['title'])}",
        f"🎯 <b>Итоговый результат:</b> {score}/{total} ({pct}%)",
        f"⏱ <b>Время решения:</b> {minutes} мин. {seconds} сек.\n",
        "<b>📋 Ответы ученика:</b>"
    ]

    for idx, a in enumerate(detailed_answers, start=1):
        status_emoji = "✅" if a.get("is_correct") else "❌"
        q_text = a.get("question", "")
        q_short = q_text[:45] + ("..." if len(q_text) > 45 else "")
        user_ans = str(a.get("user_answer", ""))
        correct_ans = str(a.get("correct_answer", ""))

        breakdown_lines.append(
            f"\n<b>{idx}. {html.escape(q_short)}</b>\n"
            f"   Выбор ученика: <code>{html.escape(user_ans)}</code> {status_emoji}\n"
            f"   Правильный ответ: <code>{html.escape(correct_ans)}</code>"
        )

    teacher_report = "\n".join(breakdown_lines)
    for admin_id in ADMIN_IDS:
        try:
            if len(teacher_report) > 4000:
                await bot.send_message(admin_id, "\n".join(breakdown_lines[:15]), parse_mode="HTML")
                await bot.send_message(admin_id, "\n".join(breakdown_lines[15:]), parse_mode="HTML")
            else:
                await bot.send_message(admin_id, teacher_report, parse_mode="HTML")
        except Exception as e:
            logger.warning("Failed to notify admin %s: %s", admin_id, e)

    return web.json_response({"success": True, "submission_id": sub_id})
