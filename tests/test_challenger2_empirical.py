"""Empirical Challenger 2 verification suite for Milestone 1.

Validates:
1. Preview output edge cases, Telegram HTML validity, and absolute absence of breakdown lines.
2. Complete absence of 'Ключ / Эталон', '.teacher-correct-ans-row', and '.tag-check' / '✓' in student answers.
3. Teacher Mini App grading functionality, legibility, and backend API contract integrity.
"""

import html
import json
import xml.etree.ElementTree as ET
import pytest
import os
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from unittest.mock import AsyncMock, MagicMock
from aiohttp.test_utils import TestClient, TestServer
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from aiogram import Bot
from handlers.admin import render_test_preview, UploadTestStates
from database import db
from services.web_server import create_web_app
import config


def validate_telegram_html(text: str) -> None:
    """Validates Telegram HTML conformance using XML parser."""
    root = ET.fromstring(f"<root>{text}</root>")
    allowed_tags = {
        "root", "b", "strong", "i", "em", "u", "ins", "s", "strike",
        "del", "span", "tg-spoiler", "a", "code", "pre", "blockquote", "tg-emoji"
    }
    for elem in root.iter():
        assert elem.tag in allowed_tags, f"Tag <{elem.tag}> is disallowed in Telegram HTML: {text}"


# =====================================================================
# 1. PREVIEW OUTPUT EDGE CASES & TELEGRAM HTML CONFORMANCE
# =====================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("raw_title", [
    "Standard English Test",
    "<script>alert('xss')</script>",
    "Advanced & Specialized <C1>",
    "Test with \"Quotes\" and 'Apostrophes'",
    "",
    None,
    "   ",
    12345,
    "_____ Fill in blanks _____",
])
async def test_preview_title_escaping_and_fallbacks(raw_title):
    expected_substr = html.escape(str(raw_title or "English Test"))
    mock_msg = MagicMock(spec=types.Message)
    mock_msg.edit_text = AsyncMock()
    mock_state = MagicMock(spec=FSMContext)
    mock_state.set_state = AsyncMock()

    payload = {
        "title": raw_title,
        "questions": [{"question_text": "Q1", "question_type": "single_choice"}]
    }

    await render_test_preview(mock_msg, payload, "regular", mock_state)
    assert mock_msg.edit_text.called
    text = mock_msg.edit_text.call_args[0][0]
    kwargs = mock_msg.edit_text.call_args[1]

    assert kwargs.get("parse_mode") == "HTML"
    assert expected_substr in text
    validate_telegram_html(text)
    assert "Опубликовать этот тест для учеников?" in text
    mock_state.set_state.assert_called_once_with(UploadTestStates.waiting_for_confirmation)


@pytest.mark.asyncio
@pytest.mark.parametrize("questions_list", [
    [],
    [{"question_text": "Q1", "image_url": "http://img.png", "question_type": "multiple_choice"}],
    [
        {"question_text": f"Q{i}", "image_url": f"http://img{i}.png", "question_type": "multiple_choice"}
        for i in range(25)
    ],
    [
        {"question_text": f"Q{i}", "question_type": "open_question"}
        for i in range(10)
    ]
])
async def test_preview_absolute_exclusion_of_breakdowns(questions_list):
    mock_msg = MagicMock(spec=types.Message)
    mock_msg.edit_text = AsyncMock()
    mock_state = MagicMock(spec=FSMContext)
    mock_state.set_state = AsyncMock()

    payload = {
        "title": "Adversarial Stress Test",
        "questions": questions_list
    }

    for test_type in ["olympiad", "regular", "unknown"]:
        mock_msg.edit_text.reset_mock()
        await render_test_preview(mock_msg, payload, test_type, mock_state)
        text = mock_msg.edit_text.call_args[0][0]

        # Breakdown lines must be 100% absent
        assert "🖼" not in text
        assert "Иллюстрации" not in text
        assert "схемы" not in text
        assert "☑️" not in text
        assert "Множественный выбор" not in text

        # Required fields must be present
        assert f"📊 <b>Всего вопросов:</b> {len(questions_list)}" in text
        cat_name = "Олимпиадный" if test_type == "olympiad" else "Обычный"
        assert f"🏷 <b>Категория:</b> {cat_name}" in text
        assert "Опубликовать этот тест для учеников?" in text
        validate_telegram_html(text)


@pytest.mark.asyncio
async def test_preview_error_handling_fallbacks():
    """Verify fallback to message.answer when edit_text raises TelegramBadRequest or Exception."""
    mock_msg = MagicMock(spec=types.Message)
    # Simulate BadRequest other than message is not modified
    mock_msg.edit_text = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="message can't be edited"))
    mock_msg.answer = AsyncMock()
    mock_state = MagicMock(spec=FSMContext)
    mock_state.set_state = AsyncMock()

    payload = {"title": "Fallback Test", "questions": []}
    await render_test_preview(mock_msg, payload, "regular", mock_state)

    assert mock_msg.answer.called
    answered_text = mock_msg.answer.call_args[0][0]
    validate_telegram_html(answered_text)
    assert "Fallback Test" in answered_text
    mock_state.set_state.assert_called_once_with(UploadTestStates.waiting_for_confirmation)


# =====================================================================
# 2. WEB ASSETS INSPECTION: ABSOLUTE EXCLUSION OF KEYS & CHECKMARKS
# =====================================================================

def test_web_assets_forbidden_elements_zero_occurrences():
    html = Path("web/teacher.html").read_text(encoding="utf-8")
    js = Path("web/teacher.js").read_text(encoding="utf-8")
    css = Path("web/teacher.css").read_text(encoding="utf-8")

    forbidden = [
        "teacher-correct-ans-row",
        "teacher-correct-ans-val",
        "Ключ / Эталон",
        "tag-check",
    ]

    for f in forbidden:
        assert f not in html, f"Found '{f}' in web/teacher.html"
        assert f not in js, f"Found '{f}' in web/teacher.js"
        assert f not in css, f"Found '{f}' in web/teacher.css"

    # Verify student answer tags in JS do not contain checkmark
    assert '<span class="tag-check">✓</span>' not in js
    assert 'class="tag-check"' not in js


def test_student_answers_css_notion_apple_styling():
    css = Path("web/teacher.css").read_text(encoding="utf-8")

    # Tokens and styling properties
    assert ".teacher-student-ans-row" in css
    assert ".teacher-student-ans-left" in css
    assert ".ans-label-dot" in css
    assert ".teacher-student-ans-label" in css
    assert ".teacher-student-ans-val" in css
    assert ".teacher-student-ans-val.empty" in css
    assert ".teacher-student-ans-tags" in css
    assert ".teacher-ans-tag" in css

    # Ensure radius-sm and border tokens are applied
    assert "border-radius: var(--radius-sm);" in css
    assert "box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);" in css


# =====================================================================
# 3. END-TO-END GRADING FUNCTIONALITY & API INTEGRITY
# =====================================================================

@pytest.fixture
def test_app():
    mock_bot = Bot(token=config.BOT_TOKEN)
    app = create_web_app(mock_bot)
    return app


@pytest.mark.asyncio
async def test_e2e_teacher_grading_workflow(test_app):
    """Verifies that grading workflow functions 100% end-to-end:
    1. Student submits answers (unrated)
    2. Teacher reviews submissions
    3. Teacher confirms grades via /api/teacher/confirm
    4. Database correctly updates score, status, and answer details
    """
    client = TestClient(TestServer(test_app))
    await client.start_server()

    teacher_id = config.ADMIN_IDS[0] if config.ADMIN_IDS else 999001
    if teacher_id not in config.ADMIN_IDS:
        config.ADMIN_IDS.append(teacher_id)

    # 1. Create a test
    test_id = await db.create_test_with_questions(
        title="Challenger2 Verification Test",
        test_type="regular",
        created_by=teacher_id,
        questions=[
            {"question_text": "Q1: Capital of UK?", "options": ["London", "Paris"], "correct_answer": "London"},
            {"question_text": "Q2: 2 + 2?", "options": ["3", "4"], "correct_answer": "4"}
        ]
    )
    questions = await db.get_test_questions(test_id)
    q1_id, q2_id = questions[0]["id"], questions[1]["id"]

    # 2. Register student and submit answers
    await db.register_student(telegram_id=888777, full_name="Student Challenger", class_name="11B")
    student_user = await db.get_user_by_telegram_id(888777)

    payload = {
        "test_id": test_id,
        "student_id": student_user["id"],
        "answers": {
            str(q1_id): "London",
            str(q2_id): "3"
        },
        "time_spent_seconds": 90
    }
    submit_res = await client.post("/api/submit", json=payload)
    assert submit_res.status == 200
    sub_data = await submit_res.json()
    submission_id = sub_data["submission_id"]

    # 3. Teacher fetches submissions
    fetch_res = await client.get(f"/api/teacher/submissions?test_id={test_id}&teacher_id={teacher_id}")
    assert fetch_res.status == 200
    subs_data = await fetch_res.json()
    assert subs_data["test"]["id"] == test_id
    matching_sub = next(s for s in subs_data["submissions"] if s["id"] == submission_id)
    assert matching_sub["status"] == "pending"
    assert matching_sub["score"] == 0

    # Verify answers have user answers and is_correct is None (not pre-judged)
    answers = matching_sub["answers"]
    assert len(answers) == 2
    assert answers[0]["user_answer"] == "London"
    assert answers[0]["is_correct"] is None
    assert answers[1]["user_answer"] == "3"
    assert answers[1]["is_correct"] is None

    # 4. Teacher grades: Q1 = Correct (true), Q2 = Incorrect (false)
    graded_answers = [
        {"question": answers[0]["question"], "user_answer": "London", "is_correct": True},
        {"question": answers[1]["question"], "user_answer": "3", "is_correct": False}
    ]
    confirm_payload = {
        "teacher_id": teacher_id,
        "submission_id": submission_id,
        "score": 1,
        "answers": graded_answers
    }
    confirm_res = await client.post("/api/teacher/confirm", json=confirm_payload)
    assert confirm_res.status == 200
    confirm_data = await confirm_res.json()
    assert confirm_data["success"] is True
    assert confirm_data["submission"]["score"] == 1
    assert confirm_data["submission"]["status"] == "confirmed"

    # 5. Verify database state
    stored_subs = await db.get_test_submissions(test_id)
    stored_sub = next(s for s in stored_subs if s["id"] == submission_id)
    assert stored_sub["status"] == "confirmed"
    assert stored_sub["score"] == 1

    stored_ans = json.loads(stored_sub["answers_json"])
    assert stored_ans[0]["is_correct"] is True
    assert stored_ans[1]["is_correct"] is False

    await client.close()
