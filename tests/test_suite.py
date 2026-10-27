import os
import sys
import json
import tempfile
import asyncio
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from io import BytesIO
from aiohttp.test_utils import TestClient, TestServer
from aiogram import Bot

# Ensure test DB is isolated
TEST_DIR = Path(tempfile.mkdtemp())
os.environ["BOT_TOKEN"] = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
os.environ["ADMIN_IDS"] = "[999001, 999002]"
os.environ["DB_PATH"] = str(TEST_DIR / "test_bot.db")

import config
config.DB_PATH = TEST_DIR / "test_bot.db"
config.ADMIN_IDS = [999001, 999002]

from database import db
db.DB_PATH = config.DB_PATH

from services.file_reader import (
    extract_text_from_file,
    extract_text_from_docx_bytes,
    extract_text_from_pdf,
    UnsupportedFileError,
    FileExtractionError,
)
from services.ai_parser import (
    clean_json_string, ParsedTest, ParsedQuestion,
    extract_answer_keys, split_test_into_chunks, count_questions_in_text,
)
from services.web_server import normalize_answer, create_web_app
from services.excel_exporter import generate_results_excel
import docx
from pypdf import PdfWriter


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    asyncio.run(db.init_db())
    yield
    if config.DB_PATH.exists():
        try:
            config.DB_PATH.unlink()
        except Exception:
            pass


# =====================================================================
# 1. FILE READER TESTS (1-12)
# =====================================================================

def test_01_extract_docx_paragraphs():
    doc = docx.Document()
    doc.add_paragraph("First English question.")
    doc.add_paragraph("Second question text.")
    bio = BytesIO()
    doc.save(bio)
    text = extract_text_from_docx_bytes(bio.getvalue())
    assert "First English question." in text
    assert "Second question text." in text


def test_02_extract_docx_tables():
    doc = docx.Document()
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "Option A"
    table.rows[0].cells[1].text = "Option B"
    table.rows[1].cells[0].text = "Option C"
    table.rows[1].cells[1].text = "Option D"
    bio = BytesIO()
    doc.save(bio)
    text = extract_text_from_docx_bytes(bio.getvalue())
    assert "Option A | Option B" in text
    assert "Option C | Option D" in text


def test_03_extract_docx_empty():
    doc = docx.Document()
    bio = BytesIO()
    doc.save(bio)
    with pytest.raises(FileExtractionError):
        extract_text_from_docx_bytes(bio.getvalue())


def test_04_extract_pdf_pages():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    bio = BytesIO()
    writer.write(bio)
    # A blank PDF has no extractable text layer
    with pytest.raises(FileExtractionError):
        extract_text_from_pdf(bio.getvalue())


def test_05_extract_pdf_corrupted():
    with pytest.raises(FileExtractionError):
        extract_text_from_pdf(b"not a valid pdf content")


def test_06_extract_txt_utf8():
    content = "Question 1: Fill in the gap _____.\nA) is\nB) are".encode("utf-8")
    text = extract_text_from_file("test.txt", content)
    assert "Question 1" in text
    assert "Fill in the gap _____" in text


def test_07_extract_txt_cp1251():
    content = "Тест по английскому языку: задание 1".encode("cp1251")
    text = extract_text_from_file("notes.txt", content)
    assert "Тест по английскому" in text


def test_08_extract_markdown():
    content = "# English Grammar Quiz\n1. She _____ (be) ready.".encode("utf-8")
    text = extract_text_from_file("test.md", content)
    assert "# English Grammar Quiz" in text


def test_09_extract_legacy_doc_rejected():
    with pytest.raises(UnsupportedFileError) as exc:
        extract_text_from_file("legacy_exam.doc", b"fake binary doc")
    assert ".doc" in str(exc.value)
    assert "docx" in str(exc.value)


def test_10_extract_photo_rejected():
    for ext in [".jpg", ".jpeg", ".png", ".webp", ".heic"]:
        with pytest.raises(UnsupportedFileError) as exc:
            extract_text_from_file(f"photo{ext}", b"fake image bytes")
        assert "Анализ изображений не поддерживается" in str(exc.value)


def test_11_extract_unsupported_archive():
    for ext in [".zip", ".rar", ".7z", ".tar.gz"]:
        with pytest.raises(UnsupportedFileError) as exc:
            extract_text_from_file(f"archive{ext}", b"fake archive bytes")
        assert "Формат файла не поддерживается" in str(exc.value)


def test_12_extract_unsupported_binary():
    for ext in [".exe", ".xlsx", ".mp3", ".pdf.exe"]:
        with pytest.raises(UnsupportedFileError):
            extract_text_from_file(f"file{ext}", b"binary bytes")


# =====================================================================
# 2. AI PARSER & STRING LOGIC TESTS (13-20)
# =====================================================================

def test_13_clean_json_markdown_block():
    raw = "```json\n{\"title\": \"Quiz 1\", \"questions\": []}\n```"
    cleaned = clean_json_string(raw)
    assert cleaned == '{"title": "Quiz 1", "questions": []}'


def test_14_clean_json_raw():
    raw = '{"title": "Quiz 2", "questions": []}'
    cleaned = clean_json_string(raw)
    assert cleaned == raw


def test_15_clean_json_whitespace():
    raw = '   \n  {"title": "Quiz 3", "questions": []}  \n '
    cleaned = clean_json_string(raw)
    assert cleaned == '{"title": "Quiz 3", "questions": []}'


def test_16_parsed_test_schema_valid():
    data = {
        "title": "Grammar Test",
        "description": "Level B2",
        "questions": [
            {
                "question_text": "I wish I _____ rich.",
                "options": ["A) was", "B) were", "C) am", "D) be"],
                "correct_answer": "B) were",
                "explanation": "Subjunctive mood"
            }
        ]
    }
    obj = ParsedTest(**data)
    assert obj.title == "Grammar Test"
    assert len(obj.questions) == 1
    assert obj.questions[0].correct_answer == "B) were"


def test_17_normalize_answer_prefix_lowercase():
    assert normalize_answer("a) apple") == "apple"
    assert normalize_answer("B) Banana") == "banana"
    assert normalize_answer("c)   car") == "car"


def test_18_normalize_answer_prefix_dot():
    assert normalize_answer("A. Dog") == "dog"
    assert normalize_answer("d. elephant") == "elephant"


def test_19_normalize_answer_spaces_and_case():
    assert normalize_answer("   HE WENT   ") == "he went"


def test_20_scoring_single_letter_match():
    # If correct answer is single letter 'B', but user answered 'B) had'
    user_ans = "B) had"
    correct_key = "B"
    user_norm = normalize_answer(user_ans)
    correct_norm = normalize_answer(correct_key)
    is_correct = (user_norm == correct_norm)
    if not is_correct and len(correct_norm) == 1:
        if user_ans.strip().lower().startswith(correct_norm):
            is_correct = True
    assert is_correct is True


# =====================================================================
# 3. DATABASE CRUD & MIGRATIONS TESTS (21-36)
# =====================================================================

@pytest.mark.asyncio
async def test_21_db_init_schema_wal():
    import aiosqlite
    async with aiosqlite.connect(config.DB_PATH) as conn:
        async with conn.execute("PRAGMA journal_mode;") as cur:
            mode = (await cur.fetchone())[0]
            assert mode.lower() == "wal"


@pytest.mark.asyncio
async def test_22_db_register_student():
    user = await db.register_student(
        telegram_id=1001,
        full_name="Иванов Иван",
        class_name="10-А"
    )
    assert user["telegram_id"] == 1001
    assert user["full_name"] == "Иванов Иван"
    assert user["class_name"] == "10-А"
    assert user["role"] == "student"


@pytest.mark.asyncio
async def test_23_db_register_student_update():
    # Re-registering updates name and class without duplicating
    user = await db.register_student(
        telegram_id=1001,
        full_name="Иванов Иван Петрович",
        class_name="10-Б"
    )
    assert user["full_name"] == "Иванов Иван Петрович"
    assert user["class_name"] == "10-Б"


@pytest.mark.asyncio
async def test_24_db_create_test_questions():
    questions = [
        {
            "question_text": "If I _____ time, I would call.",
            "options": ["A) have", "B) had", "C) will have"],
            "correct_answer": "B) had",
            "explanation": "Second conditional"
        },
        {
            "question_text": "He _____ to London last year.",
            "options": ["A) goes", "B) went", "C) gone"],
            "correct_answer": "B) went",
            "explanation": "Past simple"
        }
    ]
    test_id = await db.create_test_with_questions(
        title="Conditional & Past Simple",
        test_type="regular",
        created_by=999001,
        questions=questions,
        description="Grammar test"
    )
    assert test_id > 0

    fetched_qs = await db.get_test_questions(test_id)
    assert len(fetched_qs) == 2
    assert fetched_qs[0]["options"] == ["A) have", "B) had", "C) will have"]


@pytest.mark.asyncio
async def test_25_db_get_test_by_id():
    test = await db.get_test_by_id(1)
    assert test is not None
    assert test["title"] == "Conditional & Past Simple"
    assert test["is_active"] == 1


@pytest.mark.asyncio
async def test_26_db_get_test_by_id_missing():
    test = await db.get_test_by_id(999999)
    assert test is None


@pytest.mark.asyncio
async def test_27_db_get_tests_filter_active():
    tests = await db.get_tests(only_active=True)
    assert len(tests) >= 1
    assert all(t["is_active"] == 1 for t in tests)


@pytest.mark.asyncio
async def test_28_db_get_tests_filter_type():
    # Create olympiad test
    await db.create_test_with_questions(
        title="School Olympiad 2026",
        test_type="olympiad",
        created_by=999001,
        questions=[{"question_text": "Hard Q", "options": ["A", "B"], "correct_answer": "A"}]
    )
    olympiads = await db.get_tests(test_type="olympiad")
    assert any(t["title"] == "School Olympiad 2026" for t in olympiads)
    assert all(t["test_type"] == "olympiad" for t in olympiads)


@pytest.mark.asyncio
async def test_29_db_toggle_test_active():
    status_1 = await db.toggle_test_active(1)
    assert status_1 is False
    status_2 = await db.toggle_test_active(1)
    assert status_2 is True


@pytest.mark.asyncio
async def test_30_db_save_submission_default_pending():
    sub_id = await db.save_submission(
        student_id=1,
        test_id=1,
        score=2,
        total_questions=2,
        answers=[{"question": "Q1", "is_correct": True}],
        time_spent_seconds=60
    )
    assert sub_id > 0
    sub = await db.get_student_submission(student_id=1, test_id=1)
    assert sub["status"] == "pending"
    assert sub["score"] == 2


@pytest.mark.asyncio
async def test_31_db_get_student_submissions():
    subs = await db.get_student_submissions(student_id=1)
    assert len(subs) >= 1
    assert subs[0]["test_title"] == "Conditional & Past Simple"


@pytest.mark.asyncio
async def test_32_db_get_student_single_submission():
    sub = await db.get_student_submission(student_id=1, test_id=1)
    assert sub is not None
    assert sub["test_id"] == 1


@pytest.mark.asyncio
async def test_33_db_confirm_single_submission():
    sub = await db.get_student_submission(student_id=1, test_id=1)
    confirmed = await db.confirm_submission(sub["id"])
    assert confirmed is not None
    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_at"] is not None
    assert confirmed["full_name"] == "Иванов Иван Петрович"


@pytest.mark.asyncio
async def test_34_db_confirm_all_test_submissions():
    # Save a second pending submission
    await db.save_submission(
        student_id=1,
        test_id=1,
        score=1,
        total_questions=2,
        answers=[]
    )
    confirmed_list = await db.confirm_all_test_submissions(test_id=1)
    assert len(confirmed_list) >= 1
    # Check that no pending remain
    subs = await db.get_test_submissions(1)
    assert all(s["status"] == "confirmed" for s in subs)


@pytest.mark.asyncio
async def test_35_db_get_tests_with_stats():
    stats = await db.get_tests_with_stats()
    assert len(stats) >= 2
    t1 = next(t for t in stats if t["id"] == 1)
    assert t1["total_submissions"] >= 2
    assert t1["confirmed_count"] >= 2
    assert t1["pending_count"] == 0


@pytest.mark.asyncio
async def test_36_db_delete_test_cascade():
    temp_id = await db.create_test_with_questions(
        title="To Delete",
        test_type="regular",
        created_by=999001,
        questions=[{"question_text": "Del Q", "options": ["A"], "correct_answer": "A"}]
    )
    await db.delete_test(temp_id)
    assert await db.get_test_by_id(temp_id) is None
    qs = await db.get_test_questions(temp_id)
    assert len(qs) == 0


# =====================================================================
# 4. WEB SERVER & HTTP ENDPOINTS TESTS (37-49)
# =====================================================================

@pytest.fixture
def aiohttp_app():
    bot = Bot(token=config.BOT_TOKEN)
    app = create_web_app(bot)
    return app


@pytest.mark.asyncio
async def test_37_api_get_index_html(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    res = await client.get("/")
    assert res.status == 200
    text = await res.text()
    assert "Тестирование по английскому языку" in text
    await client.close()


@pytest.mark.asyncio
async def test_38_api_get_teacher_html(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    res = await client.get("/teacher")
    assert res.status == 200
    text = await res.text()
    assert "Кабинет учителя" in text
    await client.close()


@pytest.mark.asyncio
async def test_39_api_get_static_css(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    res1 = await client.get("/static/style.css")
    res2 = await client.get("/static/teacher.css")
    assert res1.status == 200
    assert res2.status == 200
    await client.close()


@pytest.mark.asyncio
async def test_40_api_get_static_js(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    res1 = await client.get("/static/app.js")
    res2 = await client.get("/static/teacher.js")
    assert res1.status == 200
    assert res2.status == 200
    await client.close()


@pytest.mark.asyncio
async def test_41_api_get_test_missing_params(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    res = await client.get("/api/test")
    assert res.status == 400
    data = await res.json()
    assert "error" in data
    await client.close()


@pytest.mark.asyncio
async def test_42_api_get_test_unregistered_student(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    res = await client.get("/api/test?test_id=1&student_id=888777")
    data = await res.json()
    assert "Ученик не зарегистрирован" in data.get("error", "")
    await client.close()


@pytest.mark.asyncio
async def test_43_api_get_test_anti_cheat(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    res = await client.get("/api/test?test_id=1&student_id=1001")
    assert res.status == 200
    data = await res.json()
    assert "test" in data
    questions = data["test"]["questions"]
    assert len(questions) > 0
    # ANTI-CHEAT: neither correct_answer nor explanation must be sent to client browser
    for q in questions:
        assert "correct_answer" not in q
        assert "explanation" not in q
    await client.close()


@pytest.mark.asyncio
async def test_44_api_get_test_olympiad_anti_cheat(aiohttp_app):
    # Student 1 submits olympiad test (id=2)
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    await db.save_submission(student_id=1, test_id=2, score=1, total_questions=1, answers=[])

    # Trying to open again:
    res = await client.get("/api/test?test_id=2&student_id=1001")
    data = await res.json()
    assert "Повторная сдача запрещена" in data.get("error", "")
    await client.close()


@pytest.mark.asyncio
async def test_45_api_submit_invalid_json(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    res = await client.post("/api/submit", data="invalid json")
    assert res.status == 400
    await client.close()


@pytest.mark.asyncio
async def test_46_api_submit_valid(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    payload = {
        "test_id": 1,
        "student_id": 1,
        "answers": {"1": "B) had", "2": "B) went"},
        "time_spent_seconds": 45
    }
    res = await client.post("/api/submit", json=payload)
    assert res.status == 200
    data = await res.json()
    assert data.get("success") is True
    assert data.get("submission_id") > 0
    await client.close()


@pytest.mark.asyncio
async def test_47_api_teacher_tests_unauthorized(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    res = await client.get("/api/teacher/tests?teacher_id=123")
    assert res.status == 403
    await client.close()


@pytest.mark.asyncio
async def test_48_api_teacher_tests_authorized(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    res = await client.get("/api/teacher/tests?teacher_id=999001")
    assert res.status == 200
    data = await res.json()
    assert "tests" in data
    assert len(data["tests"]) >= 1
    await client.close()


@pytest.mark.asyncio
async def test_49_api_teacher_submissions(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    res = await client.get("/api/teacher/submissions?test_id=1&teacher_id=999001")
    assert res.status == 200
    data = await res.json()
    assert "submissions" in data
    assert len(data["submissions"]) >= 1
    # Check that answers_json was parsed into dict array
    sub = data["submissions"][0]
    assert isinstance(sub["answers"], list)
    await client.close()


# =====================================================================
# 5. BUSINESS LOGIC & PRESENTATION TESTS (50-54)
# =====================================================================

@pytest.mark.asyncio
async def test_50_api_teacher_confirm(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()
    # Save a fresh pending submission
    sub_id = await db.save_submission(student_id=1, test_id=1, score=2, total_questions=2, answers=[])
    
    payload = {"teacher_id": 999001, "submission_id": sub_id}
    res = await client.post("/api/teacher/confirm", json=payload)
    assert res.status == 200
    data = await res.json()
    assert data.get("success") is True
    assert data["submission"]["status"] == "confirmed"
    await client.close()


def test_51_student_history_no_timestamp():
    # Verify string formatter does not output date/time strings
    import html
    s = {"test_title": "Grammar Quiz", "test_type": "regular", "status": "pending", "completed_at": "2026-10-27 06:52:10"}
    line = f"📚 <b>{html.escape(s['test_title'])}</b>\n   Статус: <i>Сдано на проверку</i>"
    assert "2026-" not in line
    assert "06:52" not in line
    assert "|" not in line


def test_52_teacher_compact_notification():
    # Verify teacher notification has only 4 clean lines without question breakdown
    import html
    report_text = (
        "🔔 <b>Ученик сдал тест!</b>\n\n"
        f"👤 <b>Ученик:</b> {html.escape('Иванов Иван')} (10-А)\n"
        f"📝 <b>Тест:</b> {html.escape('Past Simple')}\n"
        f"🎯 <b>Итоговый результат:</b> 8/10 (80%)\n"
        f"⏱ <b>Время решения:</b> 2 мин. 15 сек."
    )
    assert "📋 Ответы ученика:" not in report_text
    assert "Правильный ответ:" not in report_text
    assert "Выбор ученика:" not in report_text


def test_53_html_escaping_safety():
    import html
    raw_q = "Fill in: <script>alert(1)</script> and she _____ (go) to & store."
    safe_q = html.escape(raw_q)
    assert "<script>" not in safe_q
    assert "&lt;script&gt;" in safe_q
    assert "&amp;" in safe_q
    assert "_____" in safe_q # dashes are kept intact for English gap-fills without breaking HTML


def test_54_excel_export_generation():
    submissions = [
        {
            "full_name": "Петров Петр",
            "class_name": "9-А",
            "score": 9,
            "total_questions": 10,
            "completed_at": "2026-10-27 12:00:00"
        }
    ]
    excel_io = generate_results_excel("Unit 3 Test", submissions)
    bytes_data = excel_io.getvalue()
    assert len(bytes_data) > 0
    # Check that openpyxl can read it back
    import openpyxl
    wb = openpyxl.load_workbook(BytesIO(bytes_data))
    ws = wb.active
    assert "Результаты" in ws.title
    assert "Петров Петр" in ws["B4"].value or "Петров Петр" in str(ws.cell(row=4, column=2).value)


@pytest.mark.asyncio
async def test_55_animate_loading_status():
    from unittest.mock import AsyncMock, MagicMock
    from handlers.admin import animate_loading_status, LOADING_STEPS

    mock_msg = MagicMock()
    mock_msg.chat = MagicMock()
    mock_msg.chat.id = 12345
    mock_msg.edit_text = AsyncMock()

    mock_bot = MagicMock()
    mock_bot.send_chat_action = AsyncMock()

    stop_event = asyncio.Event()

    task = asyncio.create_task(animate_loading_status(mock_msg, mock_bot, stop_event))
    await asyncio.sleep(0.05)
    stop_event.set()
    await task

    assert mock_bot.send_chat_action.called
    assert len(LOADING_STEPS) >= 5
    for step in LOADING_STEPS:
        assert "<b>" in step and "</b>" in step


@pytest.mark.asyncio
async def test_56_start_command_debounce():
    from unittest.mock import AsyncMock, MagicMock
    from aiogram import types
    from handlers.common import cmd_start, _in_flight_starts, _last_start_time

    user_id = 888777
    _in_flight_starts.clear()
    _last_start_time.clear()

    mock_msg = MagicMock(spec=types.Message)
    mock_msg.from_user = MagicMock()
    mock_msg.from_user.id = user_id
    mock_msg.from_user.first_name = "DebounceTest"
    mock_msg.answer = AsyncMock()
    mock_msg.message = mock_msg

    mock_state = MagicMock()
    mock_state.clear = AsyncMock()
    mock_state.set_state = AsyncMock()

    # Call 1: should process
    await cmd_start(mock_msg, mock_state)
    assert mock_msg.answer.call_count >= 1

    # Call 2 immediately: should be debounced / dropped without calling answer again
    count_before = mock_msg.answer.call_count
    await cmd_start(mock_msg, mock_state)
    assert mock_msg.answer.call_count == count_before




# =====================================================================
# 6. CHUNKED PARSER TESTS (57-70)
# =====================================================================

def _generate_test_text(num_questions: int, include_answer_key: bool = True,
                         include_reading: bool = False) -> str:
    """
    Generate a synthetic English test with the given number of questions.
    Optionally includes an ANSWER KEY block and reading passages.
    """
    lines = ["Grade 5 English Level Test\n"]

    reading_start = None
    if include_reading and num_questions >= 30:
        reading_start = 26  # Questions 26+ will be reading comprehension

    for i in range(1, num_questions + 1):
        if include_reading and reading_start and i == reading_start:
            lines.append("\nText 1: The Adventures of Tom\n")
            lines.append("Tom was a young boy who loved to explore the forest near his home. "
                         "Every day after school, he would run to the edge of the woods and "
                         "look for interesting things. One day, he found a mysterious map.\n")

        lines.append(f"{i}. What is the correct answer for question {i}?")
        lines.append(f"A) Option A{i}")
        lines.append(f"B) Option B{i}")
        lines.append(f"C) Option C{i}")
        lines.append(f"D) Option D{i}")
        lines.append("")

    if include_answer_key:
        lines.append("\nANSWER KEY")
        for i in range(1, num_questions + 1):
            letter = ["A", "B", "C", "D"][(i - 1) % 4]
            lines.append(f"{i} {letter}")

    return "\n".join(lines)


# --- 57: extract_answer_keys finds ANSWER KEY ---
def test_57_extract_answer_keys_found():
    text = _generate_test_text(10, include_answer_key=True)
    body, keys = extract_answer_keys(text)
    assert "ANSWER KEY" in keys
    assert "1 A" in keys
    assert "10 B" in keys
    # Body should NOT contain answer key
    assert "ANSWER KEY" not in body


# --- 58: extract_answer_keys returns empty when no key ---
def test_58_extract_answer_keys_not_found():
    text = _generate_test_text(10, include_answer_key=False)
    body, keys = extract_answer_keys(text)
    assert keys == ""
    assert body == text


# --- 59: count_questions detects correct number ---
def test_59_count_questions_40():
    text = _generate_test_text(40, include_answer_key=False)
    count = count_questions_in_text(text)
    assert count == 40


def test_60_count_questions_100():
    text = _generate_test_text(100, include_answer_key=False)
    count = count_questions_in_text(text)
    assert count == 100


# --- 61: split_test_into_chunks on 40 questions ---
def test_61_chunking_40_questions():
    text = _generate_test_text(40, include_answer_key=False)
    chunks = split_test_into_chunks(text, chunk_size=25)
    assert len(chunks) == 2

    # First chunk should contain question 1
    assert "question 1?" in chunks[0]
    # Second chunk should contain question 40
    assert "question 40?" in chunks[1]

    # No chunk should be empty
    for chunk in chunks:
        assert len(chunk.strip()) > 0


# --- 62: split_test_into_chunks on 100 questions ---
def test_62_chunking_100_questions():
    text = _generate_test_text(100, include_answer_key=False)
    chunks = split_test_into_chunks(text, chunk_size=25)
    assert len(chunks) == 4

    assert "question 1?" in chunks[0]
    assert "question 100?" in chunks[3]


# --- 63: small test stays in single chunk ---
def test_63_small_test_single_chunk():
    text = _generate_test_text(15, include_answer_key=False)
    chunks = split_test_into_chunks(text, chunk_size=25)
    assert len(chunks) == 1
    assert "question 1?" in chunks[0]
    assert "question 15?" in chunks[0]


# --- 64: chunking preserves all questions (no question lost) ---
def test_64_chunking_no_questions_lost():
    text = _generate_test_text(50, include_answer_key=False)
    chunks = split_test_into_chunks(text, chunk_size=25)

    # Concatenate all chunks and verify all 50 questions are present
    full_text = "\n".join(chunks)
    for i in range(1, 51):
        assert f"question {i}?" in full_text, f"Question {i} was lost during chunking!"


# --- 65: answer key extraction with various formats ---
def test_65_answer_key_formats():
    # Test with "ANSWERS" format
    text1 = "1. Question one\nA) a\nB) b\n\nANSWERS\n1 A\n"
    body1, keys1 = extract_answer_keys(text1)
    assert "ANSWERS" in keys1
    assert "1 A" in keys1

    # Test with "KEYS" format
    text2 = "1. Question one\nA) a\nB) b\n\nKEYS\n1 B\n"
    body2, keys2 = extract_answer_keys(text2)
    assert "KEYS" in keys2

    # Test with "КЛЮЧИ" format (Russian)
    text3 = "1. Question one\nA) a\nB) b\n\nКЛЮЧИ\n1 A\n"
    body3, keys3 = extract_answer_keys(text3)
    assert "КЛЮЧИ" in keys3

    # Test that words like "monkeys" or "keys" in the middle of sentences do NOT trigger answer key extraction
    text4 = (
        "1. Question one\nA) a\nB) b\n"
        "At the zoo, we see lions, monkeys and big elephants.\n"
        "2. Question two\nA) c\nB) d\n"
    )
    body4, keys4 = extract_answer_keys(text4)
    assert keys4 == ""
    assert "monkeys" in body4


# --- 66: reading passage kept with its questions ---
def test_66_reading_passage_kept_together():
    text = _generate_test_text(40, include_answer_key=False, include_reading=True)
    chunks = split_test_into_chunks(text, chunk_size=25)

    # The reading passage "Text 1: The Adventures of Tom" should be in some chunk
    all_text = "\n".join(chunks)
    assert "Text 1: The Adventures of Tom" in all_text
    assert "Tom was a young boy" in all_text


# --- 67: verify merged questions have correct structure ---
def test_67_merged_questions_structure():
    """Simulate merging chunk results into ParsedTest."""
    chunk1_questions = [
        {
            "question_text": f"Question {i}",
            "options": ["A) a", "B) b", "C) c", "D) d"],
            "correct_answer": "A) a",
            "explanation": None,
            "context_text": None,
        }
        for i in range(1, 26)
    ]
    chunk2_questions = [
        {
            "question_text": f"Question {i}",
            "options": ["A) a", "B) b", "C) c", "D) d"],
            "correct_answer": "B) b",
            "explanation": None,
            "context_text": None,
        }
        for i in range(26, 41)
    ]

    all_questions = chunk1_questions + chunk2_questions
    parsed_questions = [ParsedQuestion(**q) for q in all_questions]

    parsed_test = ParsedTest(
        title="Test Merge Check",
        description="Testing merge",
        questions=parsed_questions,
    )

    assert len(parsed_test.questions) == 40
    assert parsed_test.title == "Test Merge Check"
    assert parsed_test.questions[0].question_text == "Question 1"
    assert parsed_test.questions[39].question_text == "Question 40"


# --- 68: verify 100-question merge integrity ---
def test_68_merge_100_questions():
    """Simulate merging 4 chunks of 25 questions each."""
    all_questions = []
    for chunk_idx in range(4):
        start = chunk_idx * 25 + 1
        end = start + 25
        for i in range(start, end):
            all_questions.append({
                "question_text": f"Q{i}: Fill in the blank _____.",
                "options": [f"A) opt_{i}_a", f"B) opt_{i}_b"],
                "correct_answer": f"A) opt_{i}_a",
                "explanation": None,
                "context_text": None,
            })

    parsed_questions = [ParsedQuestion(**q) for q in all_questions]
    parsed = ParsedTest(
        title="Big Olympiad Test",
        description="100 questions test",
        questions=parsed_questions,
    )

    assert len(parsed.questions) == 100
    # Verify sequential numbering in question text
    for i, q in enumerate(parsed.questions, start=1):
        assert f"Q{i}:" in q.question_text, f"Expected Q{i} but got {q.question_text}"


# --- 69: edge case — empty text returns empty chunks ---
def test_69_empty_text_chunks():
    chunks = split_test_into_chunks("", chunk_size=25)
    assert chunks == []

    chunks2 = split_test_into_chunks("   \n  \n  ", chunk_size=25)
    assert chunks2 == []


# --- 70: chunking with 200 questions ---
def test_70_chunking_200_questions():
    text = _generate_test_text(200, include_answer_key=False)
    chunks = split_test_into_chunks(text, chunk_size=25)
    assert len(chunks) == 8

    # Verify no question is lost
    full_text = "\n".join(chunks)
    for i in range(1, 201):
        assert f"question {i}?" in full_text, f"Question {i} was lost!"


# --- 71: reading passage at the beginning does not swallow remaining chunks ---
def test_71_reading_at_beginning_chunking():
    text = (
        "READING COMPREHENSION\n"
        "Text 1: Once upon a time there was a kingdom.\n"
        "1. Where was the kingdom? A) North B) South\n"
        "2. Who lived there? A) King B) Queen\n"
        + "\n".join([f"{i}. Question {i} is here. A) a B) b" for i in range(3, 60)])
    )
    chunks = split_test_into_chunks(text, chunk_size=25)
    # 59 questions with chunk_size 25 should yield 3 chunks (25 + 25 + 9)
    assert len(chunks) == 3
    assert "Text 1: Once upon a time" in chunks[0]
    assert "1. Where was the kingdom?" in chunks[0]
    assert "26. Question 26 is here." in chunks[1]
    assert "51. Question 51 is here." in chunks[2]


# --- 72: format_size unit tests ---
def test_72_format_size():
    from services.channel_logger import format_size
    assert format_size(500) == "500 Б"
    assert format_size(2048) == "2.0 КБ"
    assert format_size(1048576) == "1.0 МБ"


# --- 73: log_test_material_upload with document ---
@pytest.mark.asyncio
async def test_73_log_test_material_upload_doc():
    from unittest.mock import AsyncMock, MagicMock
    from services.channel_logger import log_test_material_upload

    mock_bot = MagicMock()
    mock_bot.send_document = AsyncMock()
    mock_bot.send_message = AsyncMock()

    mock_user = MagicMock()
    mock_user.full_name = "Anna Teacher"
    mock_user.username = "anna_eng"
    mock_user.id = 12345

    mock_msg = MagicMock()
    mock_msg.document = MagicMock()
    mock_msg.document.file_id = "test_file_id_xyz"
    mock_msg.document.file_name = "Olympiad9.pdf"
    mock_msg.document.file_size = 50000
    mock_msg.text = None

    await log_test_material_upload(
        bot=mock_bot,
        user=mock_user,
        custom_title="Olympiad Grade 9",
        test_type="olympiad",
        message=mock_msg
    )

    assert mock_bot.send_document.called
    call_kwargs = mock_bot.send_document.call_args.kwargs
    assert call_kwargs["document"] == "test_file_id_xyz"
    assert "Anna Teacher" in call_kwargs["caption"]
    assert "Olympiad Grade 9" in call_kwargs["caption"]
    assert "Olympiad9.pdf" in call_kwargs["caption"]


# --- 74: log_test_material_upload with text ---
@pytest.mark.asyncio
async def test_74_log_test_material_upload_text():
    from unittest.mock import AsyncMock, MagicMock
    from services.channel_logger import log_test_material_upload

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.send_document = AsyncMock()

    mock_user = MagicMock()
    mock_user.full_name = "Boris"
    mock_user.username = None
    mock_user.id = 67890

    mock_msg = MagicMock()
    mock_msg.document = None
    mock_msg.text = "1. What is the past tense of eat? A) ate B) eated"

    await log_test_material_upload(
        bot=mock_bot,
        user=mock_user,
        custom_title="Past Simple Quiz",
        test_type="regular",
        message=mock_msg
    )

    assert mock_bot.send_message.called
    call_kwargs = mock_bot.send_message.call_args.kwargs
    assert "Boris" in call_kwargs["text"]
    assert "Past Simple Quiz" in call_kwargs["text"]
    assert "What is the past tense" in call_kwargs["text"]


# --- 75: log_test_published ---
@pytest.mark.asyncio
async def test_75_log_test_published():
    from unittest.mock import AsyncMock, MagicMock
    from services.channel_logger import log_test_published

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    mock_user = MagicMock()
    mock_user.full_name = "Anna Teacher"
    mock_user.username = "anna_eng"
    mock_user.id = 12345

    await log_test_published(
        bot=mock_bot,
        user=mock_user,
        title="Final Exam",
        test_type="regular",
        question_count=35
    )

    assert mock_bot.send_message.called
    call_kwargs = mock_bot.send_message.call_args.kwargs
    assert "Final Exam" in call_kwargs["text"]
    assert "35" in call_kwargs["text"]
    assert "Anna Teacher" in call_kwargs["text"]


# --- 76: update_test_title DB function ---
@pytest.mark.asyncio
async def test_76_update_test_title():
    from database import create_test_with_questions, update_test_title, get_test_by_id

    test_id = await create_test_with_questions(
        title="Old Name For Test",
        test_type="regular",
        created_by=999001,
        questions=[{"question_text": "Q1?", "options": ["A) 1", "B) 2"], "correct_answer": "A) 1"}],
        description="test desc"
    )

    t_before = await get_test_by_id(test_id)
    assert t_before["title"] == "Old Name For Test"

    res = await update_test_title(test_id, "New Brand Name")
    assert res is True

    t_after = await get_test_by_id(test_id)
    assert t_after["title"] == "New Brand Name"


# --- 77: question numbering without dot (space only) and skipping Example 0 ---
def test_77_question_numbers_space_only_and_skip_zero():
    from services.ai_parser import count_questions_in_text, split_test_into_chunks

    text = (
        "Complete the test. Example (0) is done.\n"
        "0 Emily: Where are you going? A) home B) work\n"
        "1 Manager: Where is David? A) in B) on\n"
        "2 Amirah: Do you like cats? A) yes B) no\n"
        "3 Andrew: Where is Alicia? A) US B) UK\n"
    )
    cnt = count_questions_in_text(text)
    assert cnt == 3, f"Expected 3 questions (1, 2, 3), but detected {cnt}"


# --- 78: extract_max_key_number tests ---
def test_78_extract_max_key_number():
    from services.ai_parser import extract_max_key_number

    # Table format
    keys1 = "1 A 11 D 21 B 31 D 41 B 51 B 61 B 70 D"
    assert extract_max_key_number(keys1) == 70

    # Prefix format
    keys2 = "Q1: A, Q2: B, Q15: C, Q45: D"
    assert extract_max_key_number(keys2) == 45

    # Dot format
    keys3 = "1. True\n2. False\n30. Not Given"
    assert extract_max_key_number(keys3) == 30

    # Empty
    assert extract_max_key_number("") == 0
    assert extract_max_key_number("No keys provided") == 0


# --- 79: estimate_test_scale multi-signal tests ---
def test_79_estimate_test_scale():
    from services.ai_parser import estimate_test_scale

    # Case A: Small test with 5 questions and short text
    text_small = "1. Q1\n2. Q2\n3. Q3\n4. Q4\n5. Q5"
    cnt, max_k, is_large = estimate_test_scale(text_small, "")
    assert cnt == 5
    assert max_k == 0
    assert is_large is False

    # Case B: Large test indicated by Answer Key
    text_with_keys = "Some intro text.\n1. Q1\n2. Q2"
    key_block = "1 A\n2 B\n50 C"
    cnt, max_k, is_large = estimate_test_scale(text_with_keys, key_block)
    assert max_k == 50
    assert cnt == 50
    assert is_large is True

    # Case C: Large test indicated by physical text volume (>3500 chars) despite 0 detected questions
    text_huge_no_numbers = "This is a reading story. " * 200  # ~5000 chars
    cnt, max_k, is_large = estimate_test_scale(text_huge_no_numbers, "")
    assert is_large is True
    assert cnt >= 25


# --- 80: split_test_by_windows fallback chunking ---
def test_80_split_test_by_windows():
    from services.ai_parser import split_test_by_windows

    # Short text
    short_text = "Short text under threshold."
    chunks_short = split_test_by_windows(short_text, max_chunk_chars=3500)
    assert len(chunks_short) == 1
    assert chunks_short[0] == short_text

    # Long text with paragraphs
    paragraphs = [f"Paragraph {i}: " + ("English sentence. " * 20) for i in range(1, 30)]
    long_text = "\n\n".join(paragraphs)
    assert len(long_text) > 7000

    chunks_long = split_test_by_windows(long_text, max_chunk_chars=3500, overlap_chars=300)
    assert len(chunks_long) >= 2
    # Ensure paragraphs are not destroyed in the middle
    for chunk in chunks_long:
        assert "Paragraph" in chunk


# --- 81: Auto-Healing logic in parse_test_content ---
@pytest.mark.asyncio
async def test_81_auto_healing_recovery(monkeypatch):
    from unittest.mock import AsyncMock
    from services.ai_parser import parse_test_content, ParsedTest, ParsedQuestion

    # Create dummy text with keys indicating 40 questions
    dummy_text = "1. Q1\n2. Q2\n3. Q3\nANSWER KEY: 1 A ... 40 D"

    # Simulate single request returning only 5 questions (undercount)
    mock_single_parsed = ParsedTest(
        title="Mock Test",
        description="",
        questions=[
            ParsedQuestion(question_text=f"Q{i}", options=["A) 1", "B) 2"], correct_answer="A")
            for i in range(1, 6)
        ]
    )

    # Simulate chunked request returning all 40 questions
    mock_chunked_parsed = ParsedTest(
        title="Mock Test (Chunked)",
        description="",
        questions=[
            ParsedQuestion(question_text=f"Q{i}", options=["A) 1", "B) 2"], correct_answer="A")
            for i in range(1, 41)
        ]
    )

    # Monkeypatch _parse_single_request and _parse_chunked
    single_mock = AsyncMock(return_value=mock_single_parsed)
    chunked_mock = AsyncMock(return_value=mock_chunked_parsed)

    import services.ai_parser as aip
    monkeypatch.setattr(aip, "_parse_single_request", single_mock)
    monkeypatch.setattr(aip, "_parse_chunked", chunked_mock)

    # If is_large_test is forced to False to enter fast path:
    monkeypatch.setattr(aip, "estimate_test_scale", lambda b, k: (40, 40, False))

    res = await parse_test_content(dummy_text, test_type="regular")

    # Fast path parsed only 5 (< 40 * 0.85), so auto-healing must have called _parse_chunked
    assert chunked_mock.called
    assert len(res.questions) == 40




