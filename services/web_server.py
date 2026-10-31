import html
import json
import logging
from typing import Any, Optional, List, Dict
from aiohttp import web
from aiogram import Bot

from config import WEB_DIR, ADMIN_IDS, DATA_DIR
from database import (
    get_user_by_telegram_id,
    get_test_by_id,
    get_tests_with_stats,
    get_test_questions,
    get_student_submission,
    save_submission,
    get_test_submissions,
    confirm_submission,
    confirm_all_test_submissions,
)

logger = logging.getLogger(__name__)

def normalize_answer(text: Any) -> str:
    if isinstance(text, list):
        return ", ".join(normalize_answer(x) for x in text if x)
    t = str(text).strip().lower()
    for prefix in ["a)", "b)", "c)", "d)", "e)", "f)", "g)", "h)", "a.", "b.", "c.", "d.", "e.", "f.", "g.", "h."]:
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
            break
    return t


def is_admin(tg_id) -> bool:
    if not tg_id:
        return False
    try:
        return int(tg_id) in ADMIN_IDS
    except (ValueError, TypeError):
        return False


def create_web_app(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot

    # Static routes
    MEDIA_DIR = DATA_DIR / "tests"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/static/tests/", path=MEDIA_DIR, name="static_tests")
    app.router.add_get("/", handle_index)
    app.router.add_get("/teacher", handle_teacher_index)
    app.router.add_static("/static/", path=WEB_DIR, name="static")

    # Student API routes
    app.router.add_get("/api/test", handle_api_get_test)
    app.router.add_post("/api/submit", handle_api_submit)

    # Teacher API routes
    app.router.add_get("/api/teacher/tests", handle_api_teacher_tests)
    app.router.add_get("/api/teacher/submissions", handle_api_teacher_submissions)
    app.router.add_post("/api/teacher/confirm", handle_api_teacher_confirm)
    app.router.add_post("/api/teacher/confirm-all", handle_api_teacher_confirm_all)

    return app


async def handle_index(request: web.Request) -> web.Response:
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        return web.Response(text="index.html not found", status=404)
    return web.FileResponse(
        index_file,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


async def handle_teacher_index(request: web.Request) -> web.Response:
    teacher_file = WEB_DIR / "teacher.html"
    if not teacher_file.exists():
        return web.Response(text="teacher.html not found", status=404)
    return web.FileResponse(
        teacher_file,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


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
            "options": q.get("options", []),
            "image_url": q.get("image_url"),
            "question_type": q.get("question_type", "single_choice" if q.get("options") else "open_text")
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
    user_answers = data.get("answers", {}) # { "qid": "val" or ["val1", "val2"] }
    time_spent = int(data.get("time_spent_seconds", 0))

    if not test_id or not student_id:
        return web.json_response({"error": "Не указан test_id или student_id."}, status=400)

    test = await get_test_by_id(test_id)
    if not test:
        return web.json_response({"error": "Тест не найден."}, status=404)

    questions = await get_test_questions(test_id)
    q_map = {q["id"]: q for q in questions}

    detailed_answers = []

    for q in questions:
        qid_str = str(q["id"])
        user_ans_raw = user_answers.get(qid_str)

        if isinstance(user_ans_raw, list):
            cleaned_list = [str(x).strip() for x in user_ans_raw if str(x).strip()]
            user_ans_val = cleaned_list if cleaned_list else "(Без ответа)"
        elif isinstance(user_ans_raw, str):
            s = user_ans_raw.strip()
            user_ans_val = s if s else "(Без ответа)"
        elif user_ans_raw is None:
            user_ans_val = "(Без ответа)"
        else:
            s = str(user_ans_raw).strip()
            user_ans_val = s if s else "(Без ответа)"

        correct_raw = str(q.get("correct_answer", "")).strip()

        # NOTE: Automated AI grading is strictly disabled.
        # Questions are stored un-graded (is_correct=None). Teacher will manually verify each answer.
        detailed_answers.append({
            "question": q["question_text"],
            "user_answer": user_ans_val,
            "correct_answer": correct_raw,
            "is_correct": None,
            "explanation": q.get("explanation"),
            "image_url": q.get("image_url"),
            "question_type": q.get("question_type", "single_choice" if q.get("options") else "open_text")
        })

    total = len(questions)

    # Save to database (status is 'pending', initial score is 0 until teacher grades it)
    sub_id = await save_submission(
        student_id=student_id,
        test_id=test_id,
        score=0,
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

    # Notify teacher via Telegram (clean notification without AI scoring)
    bot: Bot = request.app["bot"]
    minutes = time_spent // 60
    seconds = time_spent % 60

    st_name = student_info["full_name"] if student_info else "Ученик"
    st_class = student_info["class_name"] if student_info else ""

    report_text = (
        "🔔 <b>Ученик сдал тест!</b>\n\n"
        f"👤 <b>Ученик:</b> {html.escape(st_name)} ({html.escape(st_class)})\n"
        f"📝 <b>Тест:</b> {html.escape(test['title'])}\n"
        f"⏱ <b>Время решения:</b> {minutes} мин. {seconds} сек.\n\n"
        "📋 <i>Работа ожидает вашей проверки в Кабинете учителя.</i>"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, report_text, parse_mode="HTML")
        except Exception as e:
            logger.warning("Failed to notify admin %s: %s", admin_id, e)

    return web.json_response({"success": True, "submission_id": sub_id})


# --- Teacher API Handlers ---

async def handle_api_teacher_tests(request: web.Request) -> web.Response:
    teacher_id_str = request.query.get("teacher_id")
    if not is_admin(teacher_id_str):
        return web.json_response({"error": "Доступ запрещен. Только для учителей бота."}, status=403)

    tests = await get_tests_with_stats()
    return web.json_response({"tests": tests})


async def handle_api_teacher_submissions(request: web.Request) -> web.Response:
    teacher_id_str = request.query.get("teacher_id")
    test_id_str = request.query.get("test_id")

    if not is_admin(teacher_id_str):
        return web.json_response({"error": "Доступ запрещен."}, status=403)

    if not test_id_str:
        return web.json_response({"error": "Не указан test_id."}, status=400)

    try:
        test_id = int(test_id_str)
    except ValueError:
        return web.json_response({"error": "Некорректный test_id."}, status=400)

    test = await get_test_by_id(test_id)
    if not test:
        return web.json_response({"error": "Тест не найден."}, status=404)

    raw_subs = await get_test_submissions(test_id)
    submissions = []
    for s in raw_subs:
        item = dict(s)
        try:
            item["answers"] = json.loads(item.get("answers_json") or "[]")
        except Exception:
            item["answers"] = []
        item.pop("answers_json", None)

        # Pending unconfirmed submissions must always be in clean unrated state (score=0, is_correct=None)
        if item.get("status") == "pending" and not item.get("confirmed_at"):
            item["score"] = 0
            for a in item["answers"]:
                a["is_correct"] = None

        submissions.append(item)

    return web.json_response({
        "test": {
            "id": test["id"],
            "title": test["title"],
            "test_type": test["test_type"]
        },
        "submissions": submissions
    })


async def handle_api_teacher_confirm(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Невалидный JSON."}, status=400)

    teacher_id = data.get("teacher_id")
    sub_id = data.get("submission_id")

    if not is_admin(teacher_id):
        return web.json_response({"error": "Доступ запрещен."}, status=403)

    if not sub_id:
        return web.json_response({"error": "Не указан submission_id."}, status=400)

    try:
        sub_id_int = int(sub_id)
    except ValueError:
        return web.json_response({"error": "Некорректный submission_id."}, status=400)

    score_val = data.get("score")
    score_int = int(score_val) if score_val is not None else None
    answers_val = data.get("answers")
    answers_list = answers_val if isinstance(answers_val, list) else None

    updated = await confirm_submission(sub_id_int, score=score_int, answers=answers_list)
    if not updated:
        return web.json_response({"error": "Сдача не найдена."}, status=404)

    # Отправляем ученику уведомление в Telegram
    bot: Bot = request.app["bot"]
    student_tg = updated.get("telegram_id")
    if student_tg:
        score = updated.get("score", 0)
        total = updated.get("total_questions", 1)
        pct = round((score / total) * 100) if total > 0 else 0
        msg = (
            "🎉 <b>Учитель проверил вашу работу!</b>\n\n"
            f"📝 <b>Тест:</b> {html.escape(updated.get('test_title', ''))}\n"
            f"🎯 <b>Ваш результат:</b> {score}/{total} ({pct}%)\n\n"
            "Оценка утверждена! Вы можете посмотреть свои работы в разделе <b>«📈 Мои результаты»</b>."
        )
        try:
            await bot.send_message(student_tg, msg, parse_mode="HTML")
        except Exception as e:
            logger.warning("Could not send confirmation to student %s: %s", student_tg, e)

    return web.json_response({"success": True, "submission": updated})


async def handle_api_teacher_confirm_all(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Невалидный JSON."}, status=400)

    teacher_id = data.get("teacher_id")
    test_id = data.get("test_id")

    if not is_admin(teacher_id):
        return web.json_response({"error": "Доступ запрещен."}, status=403)

    if not test_id:
        return web.json_response({"error": "Не указан test_id."}, status=400)

    try:
        test_id_int = int(test_id)
    except ValueError:
        return web.json_response({"error": "Некорректный test_id."}, status=400)

    confirmed_rows = await confirm_all_test_submissions(test_id_int)
    bot: Bot = request.app["bot"]

    for row in confirmed_rows:
        student_tg = row.get("telegram_id")
        if student_tg:
            score = row.get("score", 0)
            total = row.get("total_questions", 1)
            pct = round((score / total) * 100) if total > 0 else 0
            msg = (
                "🎉 <b>Учитель проверил вашу работу!</b>\n\n"
                f"📝 <b>Тест:</b> {html.escape(row.get('test_title', ''))}\n"
                f"🎯 <b>Ваш результат:</b> {score}/{total} ({pct}%)\n\n"
                "Оценка утверждена! Вы можете посмотреть свои работы в разделе <b>«📈 Мои результаты»</b>."
            )
            try:
                await bot.send_message(student_tg, msg, parse_mode="HTML")
            except Exception as e:
                logger.warning("Could not notify student %s: %s", student_tg, e)

    return web.json_response({"success": True, "confirmed_count": len(confirmed_rows)})
