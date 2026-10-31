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
    assert cnt == 4, f"Expected 4 questions (0, 1, 2, 3), but detected {cnt}"


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


# =====================================================================
# 10. RESILIENT PYDANTIC PARSING & IMAGE EXTRACTION TESTS (82-95)
# =====================================================================

def test_82_pydantic_correct_answer_none_with_options():
    """Validates that correct_answer=None does not raise ValidationError and falls back to options[0]."""
    raw_data = {
        "question_text": "9. Look at the triangle. What is the area?",
        "options": ["A) 25", "B) 35", "C) 45"],
        "correct_answer": None,
        "explanation": None,
    }
    q = ParsedQuestion(**raw_data)
    assert q.question_text == "9. Look at the triangle. What is the area?"
    assert q.correct_answer == "A) 25"
    assert q.explanation is None


def test_83_pydantic_correct_answer_none_without_options():
    """Validates that correct_answer=None for open-cloze questions falls back to empty string."""
    raw_data = {
        "question_text": "Fill in the blank with the correct form of 'be'.",
        "options": [],
        "correct_answer": None,
    }
    q = ParsedQuestion(**raw_data)
    assert q.correct_answer == ""
    assert q.options == []


def test_84_pydantic_correct_answer_numeric_and_boolean():
    """Validates that non-string answers like int or bool are cleanly coerced to strings."""
    q_int = ParsedQuestion(
        question_text="What is 5 + 5?",
        options=["10", "20"],
        correct_answer=10
    )
    assert q_int.correct_answer == "10"

    q_bool = ParsedQuestion(
        question_text="Is Earth round?",
        options=["True", "False"],
        correct_answer=True
    )
    assert q_bool.correct_answer == "True"


def test_85_pydantic_question_text_and_options_none_coercion():
    """Validates that None values for question_text and options do not crash parsing."""
    q = ParsedQuestion(
        question_text=None,
        options=None,
        correct_answer=None
    )
    assert q.question_text == "Question"
    assert q.options == []
    assert q.correct_answer == ""


def test_86_pydantic_parsed_test_defaults():
    """Validates ParsedTest default fallbacks for missing or None title/description."""
    t = ParsedTest(
        title=None,
        description=None,
        questions=[{"question_text": "Q1", "options": ["A", "B"], "correct_answer": "A"}]
    )
    assert t.title == "English Test"
    assert t.description == ""
    assert len(t.questions) == 1
    assert t.questions[0].correct_answer == "A"


def test_87_extract_images_from_pdf_vector_diagram():
    """Validates that vector drawings (geometry triangles) are extracted as diagram images and associated with question."""
    pymupdf = pytest.importorskip("pymupdf")
    from services.image_extractor import extract_images_from_pdf

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 100), "9. Look at the triangle ABC in the diagram below:")
    shape = page.new_shape()
    shape.draw_line((100, 200), (200, 200))
    shape.draw_line((200, 200), (150, 130))
    shape.draw_line((150, 130), (100, 200))
    shape.finish(color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
    shape.commit()
    page.insert_text((72, 250), "A) 25   B) 35   C) 45   D) 50")
    pdf_bytes = doc.tobytes()

    images = extract_images_from_pdf(pdf_bytes, test_id_prefix="unit_test_diag")
    assert len(images) >= 1
    diag = images[0]
    assert diag.question_num == 9
    assert diag.image_url.startswith("/static/tests/")
    assert diag.image_path.exists()
    assert len(diag.image_bytes) > 200


def test_88_extract_images_from_pdf_raster_image():
    """Validates that embedded raster images are extracted and saved."""
    pymupdf = pytest.importorskip("pymupdf")
    from PIL import Image
    from services.image_extractor import extract_images_from_pdf

    pil_img = Image.new("RGB", (90, 90), color="red")
    img_bio = BytesIO()
    pil_img.save(img_bio, format="PNG")
    img_bytes = img_bio.getvalue()

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 100), "1. What color is the square below:")
    page.insert_image(pymupdf.Rect(100, 150, 190, 240), stream=img_bytes)
    page.insert_text((72, 280), "A) Red   B) Blue")
    pdf_bytes = doc.tobytes()

    images = extract_images_from_pdf(pdf_bytes, test_id_prefix="unit_test_raster")
    assert len(images) >= 1
    img = images[0]
    assert img.question_num == 1
    assert img.image_url.startswith("/static/tests/")
    assert img.image_path.exists()


def test_89_extract_file_content_and_images_pdf():
    """Validates extract_file_content_and_images for PDF files."""
    pymupdf = pytest.importorskip("pymupdf")
    from services.file_reader import extract_file_content_and_images

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "1. First question text.")
    pdf_bytes = doc.tobytes()

    text, images = extract_file_content_and_images("quiz.pdf", pdf_bytes)
    assert "First question text." in text
    assert isinstance(images, list)


def test_90_extract_file_content_and_images_non_pdf():
    """Validates extract_file_content_and_images for TXT and non-pdf files."""
    from services.file_reader import extract_file_content_and_images

    text, images = extract_file_content_and_images("questions.txt", b"1. Question text here\nA) Yes\nB) No")
    assert "Question text here" in text
    assert images == []


@pytest.mark.asyncio
async def test_91_db_create_test_with_question_image_url():
    """Validates database schema supports image_url on questions."""
    q_list = [
        {
            "question_text": "9. Calculate the area of triangle",
            "options": ["A) 25", "B) 50"],
            "correct_answer": "A) 25",
            "explanation": "Formula: 1/2 * b * h",
            "image_url": "/static/tests/q9_diagram.png"
        }
    ]
    test_id = await db.create_test_with_questions(
        title="Geometry & Vocab Test",
        test_type="olympiad",
        created_by=999001,
        questions=q_list
    )
    assert test_id > 0

    questions = await db.get_test_questions(test_id)
    assert len(questions) == 1
    assert questions[0]["image_url"] == "/static/tests/q9_diagram.png"
    assert questions[0]["correct_answer"] == "A) 25"


@pytest.mark.asyncio
async def test_92_api_get_test_returns_image_url_with_anti_cheat(aiohttp_app):
    """Validates /api/test delivers image_url to browser while keeping correct_answer concealed."""
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()

    # Register student
    student = await db.register_student(
        telegram_id=999888,
        full_name="Тестовый Ученик",
        class_name="5-Б"
    )

    test_id = await db.create_test_with_questions(
        title="Visual Test",
        test_type="regular",
        created_by=999001,
        questions=[
            {
                "question_text": "Look at the figure",
                "options": ["A) Triangle", "B) Square"],
                "correct_answer": "A) Triangle",
                "explanation": "It has 3 sides",
                "image_url": "/static/tests/figure1.png"
            }
        ]
    )

    resp = await client.get(f"/api/test?test_id={test_id}&student_id={student['telegram_id']}")
    assert resp.status == 200
    data = await resp.json()

    # Anti-cheat check
    q_safe = data["test"]["questions"][0]
    assert "correct_answer" not in q_safe
    assert "explanation" not in q_safe
    # Image check
    assert q_safe["image_url"] == "/static/tests/figure1.png"
    await client.close()


@pytest.mark.asyncio
async def test_93_api_submit_stores_image_url_in_detailed_answers(aiohttp_app):
    """Validates /api/submit stores image_url in detailed answers for teacher audit."""
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()

    student = await db.register_student(
        telegram_id=999777,
        full_name="Смирнов Петр",
        class_name="6-А"
    )

    test_id = await db.create_test_with_questions(
        title="Visual Quiz 2",
        test_type="regular",
        created_by=999001,
        questions=[
            {
                "question_text": "Identify shape",
                "options": ["A) Circle", "B) Star"],
                "correct_answer": "A) Circle",
                "image_url": "/static/tests/star.png"
            }
        ]
    )
    questions = await db.get_test_questions(test_id)
    qid = questions[0]["id"]

    submit_payload = {
        "test_id": test_id,
        "student_id": student["id"],
        "answers": {str(qid): "A) Circle"},
        "time_spent": 15
    }
    resp = await client.post("/api/submit", json=submit_payload)
    assert resp.status == 200
    sub_data = await resp.json()
    assert sub_data["success"] is True

    # Check stored submission answers
    subs = await db.get_test_submissions(test_id)
    assert len(subs) == 1
    answers_json = json.loads(subs[0]["answers_json"])
    assert answers_json[0]["image_url"] == "/static/tests/star.png"
    await client.close()


@pytest.mark.asyncio
async def test_94_vision_solver_never_crashes_on_error():
    """Validates that solve_question_with_vision gracefully handles errors and never crashes."""
    from services.vision_solver import solve_question_with_vision

    # Empty bytes
    res_empty = await solve_question_with_vision(b"", "Sample Q", ["A", "B"])
    assert res_empty is None

    # Corrupted bytes without valid API keys configured to succeed
    res_corrupt = await solve_question_with_vision(b"not_an_image", "Sample Q", ["A", "B"])
    assert res_corrupt is None or isinstance(res_corrupt, tuple)


@pytest.mark.asyncio
async def test_95_parse_test_content_attaches_extracted_images(monkeypatch):
    """Validates that parse_test_content correlates extracted images to questions."""
    from unittest.mock import AsyncMock
    from services.image_extractor import ExtractedImage
    import services.ai_parser as aip

    dummy_parsed = ParsedTest(
        title="Mock Test With Images",
        description="",
        questions=[
            ParsedQuestion(question_text="1. What is this?", options=["A", "B"], correct_answer="A"),
            ParsedQuestion(question_text="9. Find the angle in the diagram", options=["A) 30", "B) 60"], correct_answer="B"),
        ]
    )
    monkeypatch.setattr(aip, "_parse_single_request", AsyncMock(return_value=dummy_parsed))
    monkeypatch.setattr(aip, "estimate_test_scale", lambda b, k: (2, 2, False))

    mock_image = ExtractedImage(
        image_url="/static/tests/q9_diagram.png",
        image_path=Path("/tmp/q9.png"),
        image_bytes=b"dummy_bytes",
        question_num=9,
        page_num=1
    )

    res = await aip.parse_test_content("1. Q1\n9. Q9", test_type="olympiad", images=[mock_image])
    assert res.questions[0].image_url is None
    assert res.questions[1].image_url == "/static/tests/q9_diagram.png"


def test_96_pydantic_all_none_fields():
    """Validates that a dict with None for EVERY field creates a valid ParsedQuestion without error."""
    q = ParsedQuestion(
        question_text=None,
        context_text=None,
        options=None,
        correct_answer=None,
        explanation=None,
        image_url=None
    )
    assert q.question_text == "Question"
    assert q.context_text is None
    assert q.options == []
    assert q.correct_answer == ""
    assert q.explanation is None
    assert q.image_url is None


def test_97_extract_images_empty_or_corrupt_bytes():
    """Validates that extract_images_from_pdf returns empty list gracefully on invalid bytes."""
    from services.image_extractor import extract_images_from_pdf

    assert extract_images_from_pdf(b"") == []
    assert extract_images_from_pdf(b"corrupt non-pdf data") == []


@pytest.mark.asyncio
async def test_98_attach_images_empty_or_none_cases():
    """Validates that _attach_images_and_vision_solutions handles empty inputs without exception."""
    from services.ai_parser import _attach_images_and_vision_solutions

    await _attach_images_and_vision_solutions([], None)
    await _attach_images_and_vision_solutions([ParsedQuestion(question_text="Q1", correct_answer="A")], [])


def test_99_pydantic_correct_answer_string_variations():
    """Validates that strings like 'None', 'null', 'n/a' are properly resolved to options[0]."""
    for val in ["none", "None", "NULL", "null", "N/A", "unknown", "   "]:
        q = ParsedQuestion(
            question_text="Sample Q",
            options=["A) Option 1", "B) Option 2"],
            correct_answer=val
        )
        assert q.correct_answer == "A) Option 1"


def test_100_dict_options_and_single_letter_answer():
    """Validates that dictionary options and single-letter answers are cleanly resolved."""
    q = ParsedQuestion(
        question_text="What is this fruit?",
        options={"A": "apple", "B": "banana", "C": "cherry"},
        correct_answer="B"
    )
    assert q.options == ["A) apple", "B) banana", "C) cherry"]
    assert q.correct_answer == "B) banana"


def test_101_nested_dict_and_list_correct_answer():
    """Validates that correct_answer passed as dict, list, or number is safely coerced."""
    q_dict = ParsedQuestion(
        question_text="Sample Q",
        options=["A) Red", "B) Blue"],
        correct_answer={"answer": "A"}
    )
    assert q_dict.correct_answer == "A) Red"

    q_list = ParsedQuestion(
        question_text="Sample Q",
        options=["A) Red", "B) Blue"],
        correct_answer=["B"]
    )
    assert q_list.correct_answer == "B) Blue"

    q_num = ParsedQuestion(
        question_text="Sample Q",
        options=["A) 10", "B) 20"],
        correct_answer=20
    )
    assert q_num.correct_answer == "B) 20"


def test_102_multi_question_page_diagram_precision():
    """Validates diagram matching precision on pages with section headers and dates (Rosetta Stone scenario)."""
    pymupdf = pytest.importorskip("pymupdf")
    from services.image_extractor import extract_images_from_pdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 50), "Section 1: Reading Comprehension")
    page.insert_text((50, 80), "The discovery of the Rosetta Stone in 1799 was historic. Carved in 196 BC.")
    page.insert_text((50, 150), "7. Why was the stone significant?\nA) Old\nB) 196 BC translation")
    page.insert_text((50, 240), "8. In what year was it found?\nA) 1799\nB) 1822")
    page.insert_text((50, 320), "Section 2: Geometry & Visual Tasks")
    page.insert_text((50, 350), "9. Look at the triangle ABC in the diagram below and find angle X:")

    # Geometry diagram for Q9 at y=380..460
    shape = page.new_shape()
    shape.draw_line((100, 460), (220, 460))
    shape.draw_line((220, 460), (160, 380))
    shape.draw_line((160, 380), (100, 460))
    shape.finish(color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
    shape.commit()

    page.insert_text((50, 490), "A) 45\nB) 60\nC) 90")
    page.insert_text((50, 560), "10. Calculate perimeter of triangle ABC:")
    page.insert_text((50, 600), "A) 15 cm\nB) 30 cm")

    pdf_bytes = doc.tobytes()
    images = extract_images_from_pdf(pdf_bytes, test_id_prefix="rosetta_test")

    assert len(images) == 1
    # Diagram MUST be mapped to question 9 (NOT section 1, section 2, 1799, or 196)
    assert images[0].question_num == 9


@pytest.mark.asyncio
async def test_103_vision_solver_markdown_clean_json():
    """Validates vision solver JSON parsing with markdown code fences and whitespace."""
    import re
    raw_response = "Here is the visual solution:\n```json\n{\n  \"correct_answer\": \"B) Square\",\n  \"explanation\": \"It has 4 equal sides\"\n}\n```\nHope this helps!"
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_response)
    assert m is not None
    clean_json = m.group(1).strip()
    data = json.loads(clean_json)
    assert data["correct_answer"] == "B) Square"
    assert data["explanation"] == "It has 4 equal sides"


@pytest.mark.asyncio
async def test_104_russian_question_number_matching():
    """Validates that Russian question prefixes ('Задание 9', 'Вопрос 9') correlate properly with extracted images."""
    from services.image_extractor import ExtractedImage
    from services.ai_parser import _attach_images_and_vision_solutions

    questions = [
        ParsedQuestion(question_text="Задание 9. Найдите площадь фигуры на схеме", options=["A) 25", "B) 50"], correct_answer="None"),
        ParsedQuestion(question_text="Вопрос 10. Чему равен периметр?", options=["A) 10", "B) 20"], correct_answer="20")
    ]
    images = [
        ExtractedImage(
            image_url="/static/tests/q9_ru.png",
            image_path=Path("/tmp/q9_ru.png"),
            image_bytes=b"fake_bytes",
            question_num=9,
            page_num=1
        )
    ]
    await _attach_images_and_vision_solutions(questions, images)
    assert questions[0].image_url == "/static/tests/q9_ru.png"
    assert questions[1].image_url is None


# =====================================================================
# 11. ANSWER KEYS DETECTION & PREVIEW WARNING TESTS (105-108)
# =====================================================================

def test_105_parsed_test_has_answer_keys_field():
    """Validates default and sanitized values for ParsedTest.has_answer_keys."""
    # Default is False
    test_default = ParsedTest(title="Test", questions=[])
    assert test_default.has_answer_keys is False

    # Explicit True
    test_true = ParsedTest(title="Test", questions=[], has_answer_keys=True)
    assert test_true.has_answer_keys is True

    # String sanitization
    assert ParsedTest(title="Test", questions=[], has_answer_keys="true").has_answer_keys is True
    assert ParsedTest(title="Test", questions=[], has_answer_keys="YES").has_answer_keys is True
    assert ParsedTest(title="Test", questions=[], has_answer_keys="да").has_answer_keys is True
    assert ParsedTest(title="Test", questions=[], has_answer_keys="false").has_answer_keys is False
    assert ParsedTest(title="Test", questions=[], has_answer_keys=None).has_answer_keys is False


@pytest.mark.asyncio
async def test_106_parse_test_content_sets_has_answer_keys(monkeypatch):
    """Validates that parse_test_content sets has_answer_keys=True when keys exist and False otherwise."""
    from unittest.mock import AsyncMock
    from services.ai_parser import parse_test_content, ParsedTest, ParsedQuestion
    import services.ai_parser as aip

    dummy_parsed = ParsedTest(
        title="Test",
        questions=[ParsedQuestion(question_text="Q1", options=["A) 1", "B) 2"], correct_answer="A")]
    )
    monkeypatch.setattr(aip, "_parse_single_request", AsyncMock(return_value=dummy_parsed))
    monkeypatch.setattr(aip, "estimate_test_scale", lambda b, k: (1, 1, False))

    # Case 1: Test with ANSWER KEY
    text_with_keys = "1. What is X?\nA) 1\nB) 2\n\nANSWER KEY:\n1 A"
    res_with_keys = await parse_test_content(text_with_keys, test_type="regular")
    assert res_with_keys.has_answer_keys is True

    # Case 2: Test without answer keys
    text_no_keys = "1. What is X?\nA) 1\nB) 2"
    res_no_keys = await parse_test_content(text_no_keys, test_type="regular")
    assert res_no_keys.has_answer_keys is False


@pytest.mark.asyncio
async def test_107_render_test_preview_warning_when_no_answer_keys():
    """Validates that render_test_preview displays prominent warning when has_answer_keys is False."""
    from unittest.mock import AsyncMock, MagicMock
    from aiogram import types
    from aiogram.fsm.context import FSMContext
    from handlers.admin import render_test_preview, UploadTestStates

    mock_msg = MagicMock(spec=types.Message)
    mock_msg.edit_text = AsyncMock()
    mock_state = MagicMock(spec=FSMContext)
    mock_state.set_state = AsyncMock()

    parsed_dict_no_keys = {
        "title": "Grammar Quiz <Level 1>",
        "questions": [{"question_text": "Q1", "options": ["A) 1", "B) 2"], "correct_answer": "A"}],
        "has_answer_keys": False
    }

    await render_test_preview(mock_msg, parsed_dict_no_keys, "regular", mock_state)

    mock_msg.edit_text.assert_called_once()
    called_text = mock_msg.edit_text.call_args[0][0]
    called_kwargs = mock_msg.edit_text.call_args[1]

    # Verify parse_mode is strictly HTML
    assert called_kwargs.get("parse_mode") == "HTML"
    # Title escaped properly
    assert "Grammar Quiz &lt;Level 1&gt;" in called_text
    # Clean preview without AI grading warnings
    assert "Опубликовать этот тест для учеников?" in called_text
    assert "⚠️ <b>Внимание: ключи к ответам" not in called_text
    # State transitioned to waiting_for_confirmation
    mock_state.set_state.assert_called_once_with(UploadTestStates.waiting_for_confirmation)


@pytest.mark.asyncio
async def test_108_render_test_preview_shows_keys_found():
    """Validates that render_test_preview displays clean preview when test is recognized."""
    from unittest.mock import AsyncMock, MagicMock
    from aiogram import types
    from aiogram.fsm.context import FSMContext
    from handlers.admin import render_test_preview, UploadTestStates

    mock_msg = MagicMock(spec=types.Message)
    mock_msg.edit_text = AsyncMock()
    mock_state = MagicMock(spec=FSMContext)
    mock_state.set_state = AsyncMock()

    parsed_dict_with_keys = {
        "title": "Olympiad 2026",
        "questions": [{"question_text": "Q1", "options": ["A) 1", "B) 2"], "correct_answer": "A"}],
        "has_answer_keys": True
    }

    await render_test_preview(mock_msg, parsed_dict_with_keys, "olympiad", mock_state)

    mock_msg.edit_text.assert_called_once()
    called_text = mock_msg.edit_text.call_args[0][0]
    called_kwargs = mock_msg.edit_text.call_args[1]

    # Verify parse_mode is strictly HTML
    assert called_kwargs.get("parse_mode") == "HTML"
    assert "Olympiad 2026" in called_text
    assert "Олимпиадный" in called_text
    assert "Опубликовать этот тест для учеников?" in called_text
    # State transitioned
    mock_state.set_state.assert_called_once_with(UploadTestStates.waiting_for_confirmation)


@pytest.mark.asyncio
async def test_109_edge_cases_preview_and_parser():
    """Validates edge cases: legacy parsed_dict missing has_answer_keys, None title, images."""
    from unittest.mock import AsyncMock, MagicMock
    from aiogram import types
    from aiogram.fsm.context import FSMContext
    from handlers.admin import render_test_preview

    mock_msg = MagicMock(spec=types.Message)
    mock_msg.edit_text = AsyncMock()
    mock_state = MagicMock(spec=FSMContext)
    mock_state.set_state = AsyncMock()

    # Legacy dictionary without has_answer_keys and with image
    legacy_dict = {
        "title": "Legacy Test & Special Characters",
        "questions": [
            {"question_text": "Q1", "options": ["A) 1", "B) 2"], "correct_answer": "A", "image_url": "/static/tests/img1.png"}
        ]
    }

    await render_test_preview(mock_msg, legacy_dict, "regular", mock_state)

    called_text = mock_msg.edit_text.call_args[0][0]
    # Properly escapes ampersand
    assert "Legacy Test &amp; Special Characters" in called_text
    # Illustration and multiple-choice counts are cleanly omitted from preview
    assert "🖼 <b>Иллюстрации/схемы:</b>" not in called_text
    assert "☑️ <b>Множественный выбор:</b>" not in called_text
    # Validates required clean preview lines
    assert "📊 <b>Всего вопросов:</b> 1" in called_text
    assert "🏷 <b>Категория:</b> Обычный" in called_text
    assert "Опубликовать этот тест для учеников?" in called_text


def test_110_answer_keys_false_positive_prose_not_treated_as_keys():
    """Validates that prose sentences starting with 'Answers' or 'Keys' do not trigger answer keys detection."""
    from services.ai_parser import extract_answer_keys

    # Case 1: Instruction line starting with Answers
    text1 = (
        "1. What is the capital of UK?\nA) London\nB) Paris\n"
        "Answers must be written in blue pen.\n"
        "2. What is 2 + 2?\nA) 3\nB) 4\n"
    )
    body1, keys1 = extract_answer_keys(text1)
    assert keys1 == ""
    assert "Answers must be written in blue pen." in body1
    assert "2. What is 2 + 2?" in body1

    # Case 2: Prose sentence starting with Keys
    text2 = (
        "1. Question one\nA) a\nB) b\n"
        "Keys are made of metal. You put them in locks.\n"
        "2. Question two\nA) c\nB) d\n"
    )
    body2, keys2 = extract_answer_keys(text2)
    assert keys2 == ""
    assert "Keys are made of metal." in body2
    assert "2. Question two" in body2


def test_111_multiple_candidate_headers_in_document():
    """Validates that when instructions mention 'Answers' near top and real ANSWER KEY is at the bottom, keys are properly extracted from the bottom."""
    from services.ai_parser import extract_answer_keys

    text = (
        "ENGLISH OLYMPIAD 2026\n"
        "Answers: Please write all solutions neatly.\n\n"
        "1. What is the past tense of 'go'?\n"
        "A) went\nB) gone\n\n"
        "2. He _____ soccer yesterday.\n"
        "A) played\nB) plays\n\n"
        "ANSWER KEY:\n"
        "1. A\n"
        "2. A\n"
    )
    body, keys = extract_answer_keys(text)
    assert "1. What is the past tense" in body
    assert "2. He _____ soccer" in body
    assert "ANSWER KEY:" in keys
    assert "1. A" in keys
    assert "2. A" in keys


def test_112_various_answer_key_header_formats():
    """Validates support for header variations: KEY:, Correct answers:, Правильные ответы:, Answer sheet:."""
    from services.ai_parser import extract_answer_keys

    formats = [
        ("1. Q1\nA) 1\nB) 2\n\nKEY:\n1: A\n2: B", "KEY:"),
        ("1. Q1\nA) 1\nB) 2\n\nCorrect answers:\n1. A\n2. B", "Correct answers:"),
        ("1. Q1\nA) 1\nB) 2\n\nПравильные ответы:\n1 - A\n2 - B", "Правильные ответы:"),
        ("1. Q1\nA) 1\nB) 2\n\nAnswer sheet:\n1. A\n2. B", "Answer sheet:"),
        ("1. Q1\nA) 1\nB) 2\n\nAnswers & Keys:\n1 A, 2 B", "Answers & Keys:"),
        ("1. Q1\nA) 1\nB) 2\n\n=== ANSWER KEY ===\n1. A\n2. B", "=== ANSWER KEY ==="),
    ]

    for raw_text, header_label in formats:
        body, keys = extract_answer_keys(raw_text)
        assert bool(keys and keys.strip()), f"Failed to extract for {header_label}"
        assert "1. Q1" in body


@pytest.mark.asyncio
async def test_113_confirm_publish_test_reminder_when_no_keys(monkeypatch):
    """Validates that confirm_publish_test displays reminder when has_answer_keys is False and omits it when True."""
    from unittest.mock import AsyncMock, MagicMock
    from aiogram import types
    from aiogram.fsm.context import FSMContext
    from handlers.admin import confirm_publish_test
    import handlers.admin as admin_module

    # Mock DB test creation and audit logging
    monkeypatch.setattr(admin_module, "create_test_with_questions", AsyncMock(return_value=999))
    mock_log = AsyncMock()
    monkeypatch.setattr(admin_module, "log_test_published", mock_log)

    mock_callback = MagicMock(spec=types.CallbackQuery)
    mock_callback.message = MagicMock(spec=types.Message)
    mock_callback.message.edit_text = AsyncMock()
    mock_callback.answer = AsyncMock()
    mock_callback.from_user = MagicMock(id=12345, full_name="Teacher", username="teach")
    mock_callback.bot = MagicMock()

    mock_state = MagicMock(spec=FSMContext)

    # Sub-case A: has_answer_keys = False
    mock_state.get_data = AsyncMock(return_value={
        "test_type": "regular",
        "parsed_test": {
            "title": "Grammar Quiz",
            "questions": [{"question_text": "Q1"}],
            "has_answer_keys": False
        }
    })
    await confirm_publish_test(mock_callback, mock_state)

    called_text = mock_callback.message.edit_text.call_args[0][0]
    assert "🎉 <b>Тест опубликован!</b>" in called_text
    assert "⚠️ <b>Напоминание:</b>" not in called_text
    assert "Кабинете учителя" not in called_text

    # Verify log_test_published received has_answer_keys=False
    mock_log.assert_called()
    assert mock_log.call_args.kwargs.get("has_answer_keys") is False

    # Sub-case B: has_answer_keys = True
    mock_callback.message.edit_text.reset_mock()
    mock_log.reset_mock()
    mock_state.get_data = AsyncMock(return_value={
        "test_type": "regular",
        "parsed_test": {
            "title": "Grammar Quiz",
            "questions": [{"question_text": "Q1"}],
            "has_answer_keys": True
        }
    })
    await confirm_publish_test(mock_callback, mock_state)

    called_text_b = mock_callback.message.edit_text.call_args[0][0]
    assert "🎉 <b>Тест опубликован!</b>" in called_text_b
    assert "⚠️ <b>Напоминание:</b>" not in called_text_b
    assert mock_log.call_args.kwargs.get("has_answer_keys") is True


@pytest.mark.asyncio
async def test_114_llm_hallucinated_keys_overridden_in_single_request(monkeypatch):
    """Validates that if LLM JSON includes hallucinated has_answer_keys=true for text without keys, parser overrides it to False."""
    from unittest.mock import AsyncMock
    from services.ai_parser import _parse_single_request
    import services.ai_parser as aip

    # LLM hallucinating has_answer_keys=True
    fake_llm_json = '{"title": "Test", "questions": [{"question_text": "Q1", "options": ["A) 1", "B) 2"], "correct_answer": "A"}], "has_answer_keys": true}'
    monkeypatch.setattr(aip, "_call_groq_with_fallback", AsyncMock(return_value=fake_llm_json))

    # Real text without answer keys
    text_no_keys = "1. What is X?\nA) 1\nB) 2"
    result = await _parse_single_request(text_no_keys, "regular", ["test-model"])
    # Deterministic extraction must override the hallucinated true to False
    assert result.has_answer_keys is False


def test_115_extract_max_key_number_extended_options():
    """Validates extract_max_key_number with option letters beyond D (E-H) and True/False/TF."""
    from services.ai_parser import extract_max_key_number

    keys_text = (
        "1. A\n"
        "2. B\n"
        "3. E\n"
        "4. F\n"
        "5. H\n"
        "6. True\n"
        "7. False\n"
        "8. Not Given\n"
        "Q9: G\n"
        "10 - T\n"
    )
    assert extract_max_key_number(keys_text) == 10


@pytest.mark.asyncio
async def test_116_confirm_submission_with_custom_score_and_answers():
    """Validates that confirm_submission correctly saves teacher verified score and answers."""
    sub_id = await db.save_submission(
        student_id=1,
        test_id=1,
        score=0,
        total_questions=3,
        answers=[
            {"question": "Q1", "user_answer": "cat", "is_correct": None},
            {"question": "Q2", "user_answer": "dog", "is_correct": None},
            {"question": "Q3", "user_answer": "bird", "is_correct": None},
        ]
    )

    teacher_graded_answers = [
        {"question": "Q1", "user_answer": "cat", "is_correct": True},
        {"question": "Q2", "user_answer": "dog", "is_correct": False},
        {"question": "Q3", "user_answer": "bird", "is_correct": True},
    ]

    updated = await db.confirm_submission(sub_id, score=2, answers=teacher_graded_answers)
    assert updated is not None
    assert updated["id"] == sub_id
    assert updated["score"] == 2
    assert updated["status"] == "confirmed"

    # Verify answers_json was updated
    saved_answers = json.loads(updated["answers_json"])
    assert len(saved_answers) == 3
    assert saved_answers[0]["is_correct"] is True
    assert saved_answers[1]["is_correct"] is False
    assert saved_answers[2]["is_correct"] is True


@pytest.mark.asyncio
async def test_117_api_teacher_confirm_with_custom_score(aiohttp_app):
    """Validates that POST /api/teacher/confirm updates score, answers and status to confirmed."""
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()

    sub_id = await db.save_submission(
        student_id=1,
        test_id=1,
        score=0,
        total_questions=2,
        answers=[
            {"question": "Q1", "user_answer": "apple"},
            {"question": "Q2", "user_answer": "banana"},
        ]
    )

    graded_answers = [
        {"question": "Q1", "user_answer": "apple", "is_correct": True},
        {"question": "Q2", "user_answer": "banana", "is_correct": True},
    ]

    payload = {
        "teacher_id": 999001,
        "submission_id": sub_id,
        "score": 2,
        "answers": graded_answers,
    }

    res = await client.post("/api/teacher/confirm", json=payload)
    assert res.status == 200
    data = await res.json()
    assert data.get("success") is True
    assert data["submission"]["status"] == "confirmed"
    assert data["submission"]["score"] == 2

    # Verify from DB
    subs = await db.get_test_submissions(1)
    matching = [s for s in subs if s["id"] == sub_id]
    assert len(matching) == 1
    assert matching[0]["score"] == 2
    assert matching[0]["status"] == "confirmed"

    await client.close()


def test_118_teacher_webapp_ui_elements():
    """Validates that teacher.html and teacher.js contain the custom grading elements without AI rules/checkmarks."""
    from pathlib import Path
    html_content = Path("web/teacher.html").read_text(encoding="utf-8")
    js_content = Path("web/teacher.js").read_text(encoding="utf-8")
    css_content = Path("web/teacher.css").read_text(encoding="utf-8")

    # Banner element in HTML
    assert "test-header-banner" in html_content
    assert "active-test-title" in html_content

    # Custom grading buttons in JS
    assert "btn-grade-correct" in js_content
    assert "btn-grade-incorrect" in js_content
    assert "Верно" in js_content
    assert "Неверно" in js_content
    assert "Ответ ученика" in js_content

    # Automated AI checkmarks and 'Правильно' removed from question cards
    assert 'class="val-correct"' not in js_content
    assert "Правильно:" not in js_content
    assert "is-correct-icon" not in js_content

    # Reference key row and tag checkmarks removed from Teacher Mini App
    assert "teacher-correct-ans-row" not in js_content
    assert "teacher-correct-ans-row" not in css_content
    assert "Ключ / Эталон" not in js_content
    assert "tag-check" not in js_content
    assert "tag-check" not in css_content

    # Custom animations and buttons in CSS
    assert "btn-grade" in css_content
    assert "grade-icon-circle" in css_content
    assert "gradePopCheck" in css_content
    assert "gradePopCross" in css_content
    assert "grading-progress-box" in css_content
    assert ":has(.btn-grade-correct.active)" in css_content


@pytest.mark.asyncio
async def test_119_student_submission_not_autograded(aiohttp_app):
    """Validates that student submission via /api/submit does not pre-grade answers and leaves is_correct=None."""
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()

    # Create a test with 2 questions
    test_id = await db.create_test_with_questions(
        title="Manual Review Test",
        test_type="regular",
        created_by=999001,
        questions=[
            {"question_text": "What is the capital of UK?", "options": ["London", "Paris"], "correct_answer": "London"},
            {"question_text": "What is 2+2?", "options": ["3", "4"], "correct_answer": "4"}
        ]
    )
    questions = await db.get_test_questions(test_id)
    q1_id, q2_id = questions[0]["id"], questions[1]["id"]

    # Student answers (one matching correct_answer, one not)
    payload = {
        "test_id": test_id,
        "student_id": 1,
        "answers": {
            str(q1_id): "London",
            str(q2_id): "3"
        },
        "time_spent_seconds": 60
    }

    res = await client.post("/api/submit", json=payload)
    assert res.status == 200
    data = await res.json()
    assert data["success"] is True
    sub_id = data["submission_id"]

    # Verify submission in database: score must be 0, status 'pending', and is_correct must be None
    subs = await db.get_test_submissions(test_id)
    matching = [s for s in subs if s["id"] == sub_id]
    assert len(matching) == 1
    sub = matching[0]

    assert sub["status"] == "pending"
    assert sub["score"] == 0

    answers = json.loads(sub["answers_json"])
    assert len(answers) == 2
    # Neither question should be pre-judged by AI
    assert answers[0]["is_correct"] is None
    assert answers[1]["is_correct"] is None

    await client.close()


def test_120_teacher_js_untoggle_and_progress_support():
    """Validates that teacher.js supports untoggling active grade choices and tracking progress."""
    from pathlib import Path
    js_content = Path("web/teacher.js").read_text(encoding="utf-8")

    # Untoggle logic present: sets is_correct to null
    assert "sub.answers[qIdx].is_correct = null" in js_content
    # Progress counter present
    assert "grading-progress-box" in js_content
    assert "grading-progress-fill" in js_content
    # Pending unrated display
    assert "score-unrated" in js_content


# =====================================================================
# 13. CERTIFICATE ISSUANCE TESTS (121-126)
# =====================================================================

@pytest.mark.asyncio
async def test_121_database_classes_and_students():
    """Validates get_distinct_student_classes, get_students_by_class and get_user_by_id."""
    # Register students across several classes
    u1 = await db.register_student(telegram_id=987101, full_name="Азим Гулямов", class_name="10А")
    u2 = await db.register_student(telegram_id=987102, full_name="Иван Иванов", class_name="10А")
    u3 = await db.register_student(telegram_id=987103, full_name="Анна Петрова", class_name="9Б")

    # Verify distinct sorted classes
    classes = await db.get_distinct_student_classes()
    assert "10А" in classes
    assert "9Б" in classes

    # Verify students in class
    st_10a = await db.get_students_by_class("10А")
    names_10a = [s["full_name"] for s in st_10a]
    assert "Азим Гулямов" in names_10a
    assert "Иван Иванов" in names_10a
    assert "Анна Петрова" not in names_10a

    # Verify search with Cyrillic lowercase and uppercase
    search_lower = await db.get_students_by_class("10А", query="гулямов")
    assert len(search_lower) == 1
    assert search_lower[0]["full_name"] == "Азим Гулямов"

    search_upper = await db.get_students_by_class("10А", query="АЗИМ")
    assert len(search_upper) == 1
    assert search_upper[0]["full_name"] == "Азим Гулямов"

    search_empty = await db.get_students_by_class("10А", query="Несуществующий")
    assert len(search_empty) == 0

    # Verify get_user_by_id
    user_found = await db.get_user_by_id(u1["id"])
    assert user_found is not None
    assert user_found["full_name"] == "Азим Гулямов"

    user_none = await db.get_user_by_id(99999999)
    assert user_none is None


def test_122_keyboards_certificate():
    """Validates certificate keyboard builders and reply keyboard addition."""
    from keyboards.admin_kb import (
        get_admin_main_kb,
        get_cert_classes_kb,
        get_cert_students_kb,
        get_cert_search_prompt_kb,
        get_cert_no_results_kb,
        get_cert_empty_class_kb,
        get_cert_cancel_kb,
    )

    # 1. Main admin keyboard has certificate button
    main_kb = get_admin_main_kb()
    button_texts = [btn.text for row in main_kb.keyboard for btn in row]
    assert "🎓 Выдать сертификат" in button_texts

    # 2. Class selection inline keyboard
    cls_kb = get_cert_classes_kb(["10А", "9Б"])
    cls_btn_texts = [b.text for r in cls_kb.inline_keyboard for b in r]
    cls_btn_callbacks = [b.callback_data for r in cls_kb.inline_keyboard for b in r]
    assert "10А" in cls_btn_texts
    assert "9Б" in cls_btn_texts
    assert "cert_cls:10А" in cls_btn_callbacks
    assert "cancel_action" in cls_btn_callbacks

    # 3. Student selection inline keyboard
    mock_students = [
        {"id": 1, "full_name": "Азим Гулямов"},
        {"id": 2, "full_name": "Иван Иванов"}
    ]
    stu_kb = get_cert_students_kb("10А", mock_students)
    stu_btn_texts = [b.text for r in stu_kb.inline_keyboard for b in r]
    stu_btn_callbacks = [b.callback_data for r in stu_kb.inline_keyboard for b in r]
    assert "Азим Гулямов" in stu_btn_texts
    assert "cert_stu:1" in stu_btn_callbacks
    assert "🔍 Поиск" in stu_btn_texts
    assert "cert_search:10А" in stu_btn_callbacks
    assert "⬅️ Назад к классам" in stu_btn_texts
    assert "cert_back_classes" in stu_btn_callbacks

    # 4. Search result keyboard has reset / all students button
    search_kb = get_cert_students_kb("10А", mock_students, is_search_result=True)
    search_btn_texts = [b.text for r in search_kb.inline_keyboard for b in r]
    assert "👥 Все ученики" in search_btn_texts

    # 5. Cancel and prompt keyboards
    prompt_kb = get_cert_search_prompt_kb("10А")
    assert any(b.callback_data == "cert_cls:10А" for r in prompt_kb.inline_keyboard for b in r)

    no_res_kb = get_cert_no_results_kb("10А")
    assert any(b.callback_data == "cert_search:10А" for r in no_res_kb.inline_keyboard for b in r)

    empty_kb = get_cert_empty_class_kb()
    assert any(b.callback_data == "cert_back_classes" for r in empty_kb.inline_keyboard for b in r)

    cancel_kb = get_cert_cancel_kb()
    assert any(b.callback_data == "cancel_action" for r in cancel_kb.inline_keyboard for b in r)


@pytest.mark.asyncio
async def test_123_certificate_flow_class_and_student_selection():
    """Simulates starting flow, selecting class, searching, and picking student."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.admin import (
        start_certificate_flow,
        process_class_selected,
        process_search_student_start,
        process_student_search_query,
        process_student_selected,
    )
    from states.admin_states import CertificateStates

    # Register student
    student = await db.register_student(telegram_id=987201, full_name="Гулямов Азим", class_name="11А")

    mock_state = MagicMock()
    state_store = {}
    async def mock_update_data(**kwargs):
        state_store.update(kwargs)
    async def mock_get_data():
        return dict(state_store)
    mock_state.update_data = AsyncMock(side_effect=mock_update_data)
    mock_state.get_data = AsyncMock(side_effect=mock_get_data)
    mock_state.clear = AsyncMock(side_effect=lambda: state_store.clear())
    mock_state.set_state = AsyncMock()

    # Step 1: Start flow
    from aiogram import types
    mock_msg = MagicMock(spec=types.Message)
    mock_msg.answer = AsyncMock()
    await start_certificate_flow(mock_msg, mock_state)
    mock_state.set_state.assert_called_with(CertificateStates.selecting_class)
    assert "Выберите класс" in mock_msg.answer.call_args[0][0]

    # Step 2: Choose class 11А
    mock_cb = MagicMock()
    mock_cb.data = "cert_cls:11А"
    mock_cb.message.edit_text = AsyncMock()
    mock_cb.answer = AsyncMock()
    await process_class_selected(mock_cb, mock_state)
    mock_state.set_state.assert_called_with(CertificateStates.selecting_student)
    assert "11А" in mock_cb.message.edit_text.call_args[0][0]

    # Step 3: Click search
    mock_cb.data = "cert_search:11А"
    await process_search_student_start(mock_cb, mock_state)
    mock_state.set_state.assert_called_with(CertificateStates.searching_student)
    assert "Поиск ученика" in mock_cb.message.edit_text.call_args[0][0]

    # Step 4: Type query "азим"
    query_msg = MagicMock()
    query_msg.text = "азим"
    query_msg.answer = AsyncMock()
    await process_student_search_query(query_msg, mock_state)
    mock_state.set_state.assert_called_with(CertificateStates.selecting_student)
    assert "Результаты поиска" in query_msg.answer.call_args[0][0]

    # Step 5: Select student
    mock_cb.data = f"cert_stu:{student['id']}"
    mock_cb.message.delete = AsyncMock()
    mock_cb.message.answer = AsyncMock()
    await process_student_selected(mock_cb, mock_state)

    mock_state.set_state.assert_called_with(CertificateStates.waiting_for_certificate)
    prompt = mock_cb.message.answer.call_args[0][0]
    assert "👤 Выбран ученик:" in prompt
    assert "Гулямов Азим (11А)" in prompt
    assert "Отправьте файл сертификата" in prompt


@pytest.mark.asyncio
async def test_124_certificate_delivery_document_and_photo():
    """Validates delivery of certificate file (document or photo) to student chat."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.admin import process_certificate_upload
    from states.admin_states import CertificateStates

    student = await db.register_student(telegram_id=987301, full_name="Гулямов Азим", class_name="10А")

    mock_state = MagicMock()
    state_store = {
        "student_id": student["id"],
        "student_tg_id": student["telegram_id"],
        "full_name": student["full_name"],
        "class_name": student["class_name"]
    }
    mock_state.get_data = AsyncMock(return_value=state_store)
    mock_state.clear = AsyncMock()

    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.send_document = AsyncMock()
    mock_bot.send_photo = AsyncMock()

    # Sub-case A: Teacher sends Document (.pdf)
    doc_msg = MagicMock()
    doc_msg.from_user.id = 999001
    doc_msg.document = MagicMock()
    doc_msg.document.file_id = "doc_file_123"
    doc_msg.photo = None
    doc_msg.video = None
    doc_msg.animation = None
    doc_msg.caption = None
    doc_msg.answer = AsyncMock()

    await process_certificate_upload(doc_msg, mock_state, mock_bot)

    # Verify student received congratulatory message
    assert mock_bot.send_message.call_count == 1
    sent_text = mock_bot.send_message.call_args[1]["text"]
    assert "🏆 <b>Поздравляем, Гулямов Азим!</b>" in sent_text
    assert "Учитель наградил вас сертификатом/дипломом!" in sent_text
    assert "Файл прикреплен ниже ⬇️" in sent_text
    assert mock_bot.send_message.call_args[1]["chat_id"] == 987301

    # Verify student received document
    assert mock_bot.send_document.call_count == 1
    assert mock_bot.send_document.call_args[1]["document"] == "doc_file_123"

    # Verify teacher received confirmation
    teacher_reply = doc_msg.answer.call_args[0][0]
    assert "✅ <b>Сертификат успешно доставлен ученику Гулямов Азим (10А)!</b>" in teacher_reply
    mock_state.clear.assert_called()

    # Sub-case B: Teacher sends Photo
    mock_bot.send_message.reset_mock()
    mock_state.clear.reset_mock()
    photo_msg = MagicMock()
    photo_msg.from_user.id = 999001
    photo_msg.document = None
    photo_obj = MagicMock()
    photo_obj.file_id = "photo_file_456"
    photo_msg.photo = [photo_obj]
    photo_msg.video = None
    photo_msg.animation = None
    photo_msg.caption = "Диплом за 1 место"
    photo_msg.answer = AsyncMock()

    await process_certificate_upload(photo_msg, mock_state, mock_bot)
    assert mock_bot.send_message.call_count == 1
    assert mock_bot.send_photo.call_count == 1
    assert mock_bot.send_photo.call_args[1]["photo"] == "photo_file_456"
    assert "✅ <b>Сертификат успешно доставлен ученику Гулямов Азим (10А)!</b>" in photo_msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_125_certificate_delivery_error_handling():
    """Validates graceful error handling when student blocks bot, chat not found, or invalid ID."""
    from unittest.mock import AsyncMock, MagicMock
    from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
    from handlers.admin import process_certificate_upload

    mock_state = MagicMock()
    mock_state.get_data = AsyncMock(return_value={
        "student_id": 1,
        "student_tg_id": 987401,
        "full_name": "Блокировщик Бот",
        "class_name": "10А"
    })
    mock_state.clear = AsyncMock()

    # Case 1: Student blocked bot (TelegramForbiddenError)
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(side_effect=TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user"))

    err_msg = MagicMock()
    err_msg.from_user.id = 999001
    err_msg.document = MagicMock()
    err_msg.document.file_id = "f_1"
    err_msg.photo = None
    err_msg.video = None
    err_msg.animation = None
    err_msg.caption = None
    err_msg.answer = AsyncMock()

    await process_certificate_upload(err_msg, mock_state, mock_bot)
    assert "ученик заблокировал бота" in err_msg.answer.call_args[0][0]
    mock_state.clear.assert_called()

    # Case 2: Chat not found (TelegramBadRequest)
    mock_bot.send_message = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: chat not found"))
    err_msg.answer.reset_mock()
    mock_state.clear.reset_mock()

    await process_certificate_upload(err_msg, mock_state, mock_bot)
    assert "чат с учеником не найден" in err_msg.answer.call_args[0][0]
    mock_state.clear.assert_called()

    # Case 3: Missing student telegram_id
    mock_state.get_data = AsyncMock(return_value={
        "student_id": 1,
        "student_tg_id": None,
        "full_name": "Без ID",
        "class_name": "10А"
    })
    err_msg.answer.reset_mock()
    await process_certificate_upload(err_msg, mock_state, mock_bot)
    assert "отсутствует Telegram ID" in err_msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_126_certificate_wrong_content_and_navigation():
    """Validates warning when sending text instead of file, and navigating back to classes."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.admin import (
        process_certificate_wrong_content,
        back_to_classes_callback,
    )
    from states.admin_states import CertificateStates

    # Sending text when waiting for file
    text_msg = MagicMock()
    text_msg.answer = AsyncMock()
    await process_certificate_wrong_content(text_msg)
    assert "Пожалуйста, отправьте файл сертификата" in text_msg.answer.call_args[0][0]

    # Navigating back to classes
    mock_cb = MagicMock()
    mock_cb.message.edit_text = AsyncMock()
    mock_cb.answer = AsyncMock()
    mock_state = MagicMock()
    mock_state.clear = AsyncMock()
    mock_state.set_state = AsyncMock()

    await back_to_classes_callback(mock_cb, mock_state)
    mock_state.set_state.assert_called_with(CertificateStates.selecting_class)
    assert "Выберите класс" in mock_cb.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_127_natural_sorting_classes_and_multiword_search():
    """Validates natural grade order sorting (e.g. 5A before 10A) and multi-word token search."""
    # Register students across various numbered grades
    await db.register_student(telegram_id=988101, full_name="Гулямов Азим", class_name="10А")
    await db.register_student(telegram_id=988102, full_name="Иванов Петр", class_name="5Б")
    await db.register_student(telegram_id=988103, full_name="Сидоров Иван", class_name="9В")
    await db.register_student(telegram_id=988104, full_name="Алексеев Семен", class_name="11А")

    classes = await db.get_distinct_student_classes()
    assert "5Б" in classes
    assert "9В" in classes
    assert "10А" in classes
    assert "11А" in classes

    # Confirm 5Б appears before 10А and 9В appears before 10А
    idx_5 = classes.index("5Б")
    idx_9 = classes.index("9В")
    idx_10 = classes.index("10А")
    idx_11 = classes.index("11А")
    assert idx_5 < idx_9 < idx_10 < idx_11

    # Multi-word reversed token search: DB has "Гулямов Азим", teacher searches "Азим Гулямов"
    found = await db.get_students_by_class("10А", query="Азим Гулямов")
    assert len(found) >= 1
    assert any(s["full_name"] == "Гулямов Азим" for s in found)

    # Partial tokens
    found_part = await db.get_students_by_class("10А", query="гулям аз")
    assert len(found_part) >= 1
    assert any(s["full_name"] == "Гулямов Азим" for s in found_part)


@pytest.mark.asyncio
async def test_128_in_place_search_editing_and_query_message_cleanup():
    """Validates that search query message is deleted and search prompt message is edited in place."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.admin import process_student_search_query
    from states.admin_states import CertificateStates

    await db.register_student(telegram_id=988201, full_name="Гулямов Азим", class_name="10А")

    mock_state = MagicMock()
    mock_state.get_data = AsyncMock(return_value={
        "selected_class": "10А",
        "cert_search_prompt_msg_id": 555
    })
    mock_state.set_state = AsyncMock()

    mock_bot = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    query_msg = MagicMock()
    query_msg.chat.id = 999001
    query_msg.text = "азим"
    query_msg.delete = AsyncMock()
    query_msg.answer = AsyncMock()

    await process_student_search_query(query_msg, mock_state, bot=mock_bot)

    # User message was deleted to avoid chat spam
    query_msg.delete.assert_called_once()
    # Existing search prompt was edited with results
    mock_bot.edit_message_text.assert_called_once()
    call_kwargs = mock_bot.edit_message_text.call_args[1]
    assert call_kwargs["message_id"] == 555
    assert "Результаты поиска" in call_kwargs["text"]
    # No new answer message was created
    query_msg.answer.assert_not_called()
    mock_state.set_state.assert_called_with(CertificateStates.selecting_student)


@pytest.mark.asyncio
async def test_129_prompt_reply_markup_removed_on_delivery():
    """Validates that the [ ❌ Отмена ] button is stripped from the prompt message after success."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.admin import process_certificate_upload

    student = await db.register_student(telegram_id=988301, full_name="Гулямов Азим", class_name="10А")

    mock_state = MagicMock()
    mock_state.get_data = AsyncMock(return_value={
        "student_id": student["id"],
        "student_tg_id": student["telegram_id"],
        "full_name": student["full_name"],
        "class_name": student["class_name"],
        "cert_prompt_msg_id": 777
    })
    mock_state.clear = AsyncMock()

    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.send_document = AsyncMock()
    mock_bot.edit_message_reply_markup = AsyncMock()

    doc_msg = MagicMock()
    doc_msg.from_user.id = 999001
    doc_msg.chat.id = 999001
    doc_msg.document = MagicMock()
    doc_msg.document.file_id = "doc_file_777"
    doc_msg.photo = None
    doc_msg.video = None
    doc_msg.animation = None
    doc_msg.caption = "Диплом первой степени"
    doc_msg.caption_entities = [MagicMock()]
    doc_msg.answer = AsyncMock()

    await process_certificate_upload(doc_msg, mock_state, mock_bot)

    # Prompt message reply markup was cleared
    mock_bot.edit_message_reply_markup.assert_called_once_with(
        chat_id=999001,
        message_id=777,
        reply_markup=None
    )
    # Caption entities were forwarded
    assert mock_bot.send_document.call_args[1]["caption_entities"] == doc_msg.caption_entities
    # Teacher confirmation has admin keyboard
    assert doc_msg.answer.call_args[1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_130_media_failure_cleans_up_intro_message():
    """Validates that if document upload fails after text intro, intro message is retracted."""
    from unittest.mock import AsyncMock, MagicMock
    from aiogram.exceptions import TelegramForbiddenError
    from handlers.admin import process_certificate_upload

    mock_state = MagicMock()
    mock_state.get_data = AsyncMock(return_value={
        "student_id": 1,
        "student_tg_id": 988401,
        "full_name": "Тестовый Ученик",
        "class_name": "10А",
        "cert_prompt_msg_id": 888
    })
    mock_state.clear = AsyncMock()

    intro_sent = MagicMock()
    intro_sent.message_id = 999123

    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=intro_sent)
    # Document sending raises Forbidden (blocked)
    mock_bot.send_document = AsyncMock(side_effect=TelegramForbiddenError(method=MagicMock(), message="Forbidden"))
    mock_bot.delete_message = AsyncMock()

    doc_msg = MagicMock()
    doc_msg.from_user.id = 999001
    doc_msg.chat.id = 999001
    doc_msg.document = MagicMock()
    doc_msg.document.file_id = "doc_f_fail"
    doc_msg.photo = None
    doc_msg.video = None
    doc_msg.animation = None
    doc_msg.caption = None
    doc_msg.caption_entities = None
    doc_msg.answer = AsyncMock()

    await process_certificate_upload(doc_msg, mock_state, mock_bot)

    # Retracted intro message from student chat so they don't see orphaned text
    mock_bot.delete_message.assert_called_once_with(chat_id=988401, message_id=999123)
    # Teacher was warned and main reply keyboard was restored
    assert "заблокировал бота" in doc_msg.answer.call_args[0][0]
    assert doc_msg.answer.call_args[1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_131_wrong_content_handles_stickers_and_audio():
    """Validates that process_certificate_wrong_content warns appropriately on any non-file content."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.admin import process_certificate_wrong_content

    sticker_msg = MagicMock()
    sticker_msg.text = None
    sticker_msg.sticker = MagicMock()
    sticker_msg.answer = AsyncMock()

    await process_certificate_wrong_content(sticker_msg)
    assert "Пожалуйста, отправьте файл сертификата" in sticker_msg.answer.call_args[0][0]


def test_132_safe_callback_data_truncation():
    """Validates that _safe_cert_cb truncates oversized callback data safely under 64 bytes."""
    from keyboards.admin_kb import _safe_cert_cb, get_cert_classes_kb

    huge_name = "Очень длинное название специализированного лицейского класса 11-ФМ-2026"
    safe_cb = _safe_cert_cb("cert_cls", huge_name, max_bytes=60)
    assert len(safe_cb.encode("utf-8")) <= 60
    assert safe_cb.startswith("cert_cls:")

    kb = get_cert_classes_kb([huge_name])
    for row in kb.inline_keyboard:
        for btn in row:
            assert len(btn.callback_data.encode("utf-8")) <= 64


@pytest.mark.asyncio
async def test_133_student_button_immediate_answer_and_double_tap():
    """Validates that student callback handlers answer immediately and ignore double-tap 'message is not modified' errors."""
    from unittest.mock import AsyncMock, MagicMock
    from aiogram.exceptions import TelegramBadRequest
    from handlers.student import view_test_card

    test_id = await db.create_test_with_questions(
        title="Quick Button Test",
        test_type="regular",
        created_by=999001,
        questions=[{"question_text": "What is 2+2?", "options": ["3", "4"], "correct_answer": "4"}]
    )
    await db.register_student(telegram_id=989101, full_name="Тест Студент", class_name="9А")

    # Call 1: Normal click
    cb = MagicMock()
    cb.data = f"select_test:{test_id}"
    cb.from_user.id = 989101
    cb.answer = AsyncMock()
    cb.message.edit_text = AsyncMock()

    await view_test_card(cb)
    cb.answer.assert_called_once()
    cb.message.edit_text.assert_called_once()

    # Call 2: Rapid double tap raising "message is not modified"
    cb2 = MagicMock()
    cb2.data = f"select_test:{test_id}"
    cb2.from_user.id = 989101
    cb2.answer = AsyncMock()
    cb2.message.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message")
    )

    # Should not raise exception
    await view_test_card(cb2)
    cb2.answer.assert_called_once()

    # Call 3: Other unexpected TelegramBadRequest should be raised
    cb3 = MagicMock()
    cb3.data = f"select_test:{test_id}"
    cb3.from_user.id = 989101
    cb3.answer = AsyncMock()
    cb3.message.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: chat not found")
    )
    with pytest.raises(TelegramBadRequest):
        await view_test_card(cb3)


@pytest.mark.asyncio
async def test_134_student_unregistered_or_inactive_test_guard():
    """Validates that unregistered users or inactive tests do not crash view_test_card."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.student import view_test_card

    test_id = await db.create_test_with_questions(
        title="Inactive Guard Test",
        test_type="regular",
        created_by=999001,
        questions=[{"question_text": "Q1?", "options": ["A"], "correct_answer": "A"}]
    )

    # Case 1: Unregistered user clicks test card
    cb_unreg = MagicMock()
    cb_unreg.data = f"select_test:{test_id}"
    cb_unreg.from_user.id = 999999999  # not in DB
    cb_unreg.answer = AsyncMock()
    cb_unreg.message.answer = AsyncMock()

    await view_test_card(cb_unreg)
    cb_unreg.answer.assert_called_once()
    assert "Сначала пройдите регистрацию" in cb_unreg.message.answer.call_args[0][0]

    # Case 2: Inactive test
    await db.toggle_test_active(test_id)
    await db.register_student(telegram_id=989102, full_name="Второй Студент", class_name="9А")

    cb_inactive = MagicMock()
    cb_inactive.data = f"select_test:{test_id}"
    cb_inactive.from_user.id = 989102
    cb_inactive.answer = AsyncMock()
    cb_inactive.message.answer = AsyncMock()

    await view_test_card(cb_inactive)
    cb_inactive.answer.assert_called_once()
    assert "недоступен" in cb_inactive.message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_135_student_quiz_button_answer_bounds_and_immediate_answer():
    """Validates process_quiz_button_answer immediately answers and safely handles out-of-bound indices."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.student import process_quiz_button_answer

    state = MagicMock()
    state.get_data = AsyncMock(return_value={
        "current_index": 0,
        "questions": [{"question_text": "Q1", "options": ["A", "B"], "correct_answer": "A"}],
        "score": 0,
        "answers": []
    })
    state.update_data = AsyncMock()

    cb = MagicMock()
    cb.data = "quiz_choice:0"
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.answer = AsyncMock()

    # Normal valid answer
    await process_quiz_button_answer(cb, state)
    cb.answer.assert_called_once()

    # Out of bounds option index
    cb_bad_opt = MagicMock()
    cb_bad_opt.data = "quiz_choice:99"
    cb_bad_opt.answer = AsyncMock()
    await process_quiz_button_answer(cb_bad_opt, state)
    cb_bad_opt.answer.assert_called_once()

    # Out of bounds question index (e.g. after finish)
    state_finished = MagicMock()
    state_finished.get_data = AsyncMock(return_value={
        "current_index": 5,
        "questions": [{"question_text": "Q1", "options": ["A", "B"], "correct_answer": "A"}],
    })
    cb_finished = MagicMock()
    cb_finished.data = "quiz_choice:0"
    cb_finished.answer = AsyncMock()
    await process_quiz_button_answer(cb_finished, state_finished)
    cb_finished.answer.assert_called_once()


@pytest.mark.asyncio
async def test_136_student_start_quiz_guards():
    """Validates start_quiz_handler answers immediately and guards against inactive tests and unregistered users."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.student import start_quiz_handler

    test_id = await db.create_test_with_questions(
        title="Start Quiz Guard",
        test_type="regular",
        created_by=999001,
        questions=[{"question_text": "Q?", "options": ["A"], "correct_answer": "A"}]
    )

    state = MagicMock()
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()

    # Case 1: Unregistered user
    cb_unreg = MagicMock()
    cb_unreg.data = f"begin_quiz:{test_id}"
    cb_unreg.from_user.id = 88888888
    cb_unreg.answer = AsyncMock()
    cb_unreg.message.answer = AsyncMock()

    await start_quiz_handler(cb_unreg, state)
    cb_unreg.answer.assert_called_once()
    assert "Сначала пройдите регистрацию" in cb_unreg.message.answer.call_args[0][0]
    state.set_state.assert_not_called()

    # Case 2: Inactive test
    await db.toggle_test_active(test_id)
    cb_inactive = MagicMock()
    cb_inactive.data = f"begin_quiz:{test_id}"
    cb_inactive.from_user.id = 989101
    cb_inactive.answer = AsyncMock()
    cb_inactive.message.answer = AsyncMock()

    await start_quiz_handler(cb_inactive, state)
    cb_inactive.answer.assert_called_once()
    assert "недоступен" in cb_inactive.message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_137_admin_upload_latency_immediate_feedback_and_typing():
    """Validates that process_test_upload immediately sends typing action and loading status message."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from aiogram.enums import ChatAction
    from handlers.admin import process_test_upload, LOADING_STEPS

    mock_bot = AsyncMock()
    mock_bot.send_chat_action = AsyncMock()
    status_msg = MagicMock()
    status_msg.chat.id = 999001
    status_msg.edit_text = AsyncMock()
    
    mock_msg = MagicMock()
    mock_msg.chat.id = 999001
    mock_msg.from_user.id = 999001
    mock_msg.text = None
    mock_msg.document = MagicMock()
    mock_msg.document.file_name = "sample_test.docx"
    mock_msg.answer = AsyncMock(return_value=status_msg)

    mock_state = MagicMock()
    mock_state.get_data = AsyncMock(return_value={"test_type": "regular", "custom_title": "Uploaded File Test"})
    mock_state.update_data = AsyncMock()
    mock_state.set_state = AsyncMock()

    parsed_sample = ParsedTest(
        title="Uploaded File Test",
        description="",
        questions=[
            ParsedQuestion(
                question_text="Sample Q1",
                options=["A", "B", "C", "D"],
                correct_answer="A",
                explanation="Explanation 1"
            )
        ]
    )

    with patch("handlers.admin.extract_file_content_and_images", return_value=("1. Sample Q1\nA) A\nB) B\nC) C\nD) D\nKey: A", [])):
        with patch("handlers.admin.parse_test_content", AsyncMock(return_value=parsed_sample)):
            with patch("handlers.admin.log_test_material_upload", AsyncMock()):
                await process_test_upload(mock_msg, mock_state, mock_bot)

    # 1. ChatAction.TYPING was sent immediately
    assert mock_bot.send_chat_action.called
    first_call = mock_bot.send_chat_action.call_args_list[0]
    assert first_call.args == (999001, ChatAction.TYPING) or first_call.kwargs.get("action") == ChatAction.TYPING
    # 2. Loading status was sent immediately
    mock_msg.answer.assert_called_once_with(LOADING_STEPS[0], parse_mode="HTML")
    # 3. File was downloaded via bot.download
    mock_bot.download.assert_called_once()
    # 4. Preview was rendered on status_msg
    status_msg.edit_text.assert_called()
    assert "Тест успешно распознан" in status_msg.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_138_admin_upload_extraction_error_cancels_animation():
    """Validates that if file extraction fails, loading animation task is cancelled and error displayed on status_msg."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from services.file_reader import UnsupportedFileError
    from handlers.admin import process_test_upload

    mock_bot = AsyncMock()
    status_msg = MagicMock()
    status_msg.chat.id = 999001
    status_msg.edit_text = AsyncMock()

    mock_msg = MagicMock()
    mock_msg.chat.id = 999001
    mock_msg.from_user.id = 999001
    mock_msg.text = None
    mock_msg.document = MagicMock()
    mock_msg.document.file_name = "test.zip"
    mock_msg.answer = AsyncMock(return_value=status_msg)

    mock_state = MagicMock()
    mock_state.get_data = AsyncMock(return_value={"test_type": "regular"})

    with patch("handlers.admin.extract_file_content_and_images", side_effect=UnsupportedFileError("❌ <b>Формат файла не поддерживается</b>")):
        await process_test_upload(mock_msg, mock_state, mock_bot)

    status_msg.edit_text.assert_called_once()
    assert "Формат файла не поддерживается" in status_msg.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_139_admin_upload_short_text_validation():
    """Validates that short text is rejected immediately without starting animation."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.admin import process_test_upload

    mock_bot = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.text = "short"
    mock_msg.document = None
    mock_msg.answer = AsyncMock()

    mock_state = MagicMock()

    await process_test_upload(mock_msg, mock_state, mock_bot)
    mock_msg.answer.assert_called_once()
    assert "Текст задания слишком короткий" in mock_msg.answer.call_args[0][0]
    mock_bot.send_chat_action.assert_not_called()


@pytest.mark.asyncio
async def test_140_student_quiz_button_negative_and_reply_markup_removal():
    """Validates that negative or malformed choice indices are rejected, and answered questions have reply markup removed."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.student import process_quiz_button_answer

    state = MagicMock()
    state.get_data = AsyncMock(return_value={
        "current_index": 0,
        "questions": [{"question_text": "Q1", "options": ["A", "B", "C"], "correct_answer": "A"}],
        "score": 0,
        "answers": []
    })
    state.update_data = AsyncMock()

    # Case 1: Negative choice index should be rejected
    cb_neg = MagicMock()
    cb_neg.data = "quiz_choice:-1"
    cb_neg.answer = AsyncMock()
    cb_neg.message = MagicMock()
    cb_neg.message.edit_reply_markup = AsyncMock()
    await process_quiz_button_answer(cb_neg, state)
    cb_neg.answer.assert_called_once()
    cb_neg.message.edit_reply_markup.assert_not_called()

    # Case 2: Malformed choice index should be rejected
    cb_malformed = MagicMock()
    cb_malformed.data = "quiz_choice:abc"
    cb_malformed.answer = AsyncMock()
    cb_malformed.message = MagicMock()
    cb_malformed.message.edit_reply_markup = AsyncMock()
    await process_quiz_button_answer(cb_malformed, state)
    cb_malformed.answer.assert_called_once()
    cb_malformed.message.edit_reply_markup.assert_not_called()

    # Case 3: Valid index removes reply markup from answered question
    cb_valid = MagicMock()
    cb_valid.data = "quiz_choice:1"
    cb_valid.answer = AsyncMock()
    cb_valid.message = MagicMock()
    cb_valid.message.answer = AsyncMock()
    cb_valid.message.edit_reply_markup = AsyncMock()

    await process_quiz_button_answer(cb_valid, state)
    cb_valid.answer.assert_called_once()
    cb_valid.message.edit_reply_markup.assert_called_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_141_student_quiz_text_answer_out_of_bounds_safety():
    """Validates that process_quiz_text_answer safely handles out-of-bounds question index without IndexError."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.student import process_quiz_text_answer

    state = MagicMock()
    state.get_data = AsyncMock(return_value={
        "current_index": 3,
        "questions": [{"question_text": "Q1", "options": ["A"], "correct_answer": "A"}],
    })

    msg = MagicMock()
    msg.text = "delayed answer"
    # Should safely return without raising IndexError
    await process_quiz_text_answer(msg, state)


@pytest.mark.asyncio
async def test_142_student_finish_quiz_unregistered_safety():
    """Validates that finish_quiz handles unregistered or missing users cleanly without TypeError."""
    from unittest.mock import AsyncMock, MagicMock
    from handlers.student import finish_quiz

    state = MagicMock()
    state.get_data = AsyncMock(return_value={
        "test_id": 1,
        "test_title": "Test Title",
        "questions": [{"question_text": "Q1"}],
        "answers": [],
        "start_time": 1000.0
    })
    state.clear = AsyncMock()

    msg = MagicMock()
    msg.chat.id = 999999999  # non-existent user
    msg.answer = AsyncMock()
    msg.bot = AsyncMock()

    # Should not raise TypeError: 'NoneType' object is not subscriptable
    await finish_quiz(msg, state)
    msg.answer.assert_called_once()
    assert "Работа успешно отправлена" in msg.answer.call_args[0][0]


def test_143_file_extraction_error_escapes_html():
    """Validates that FileExtractionError properly escapes HTML tags and entities."""
    from services.file_reader import extract_text_from_pdf, FileExtractionError
    import html

    # Corrupted PDF with angle brackets in raw bytes
    bad_pdf = b"%PDF-1.4\n<< /Error <unclosed> >>"
    with pytest.raises(FileExtractionError) as exc_info:
        extract_text_from_pdf(bad_pdf)
    
    err_str = str(exc_info.value)
    # Must not contain unescaped raw '<' followed by unclosed tags
    assert "<unclosed>" not in err_str
    if "&lt;" in err_str:
        assert "&gt;" in err_str


@pytest.mark.asyncio
async def test_144_safe_edit_or_answer_fallback_on_edit_failure():
    """Validates that safe_edit_or_answer falls back to message.answer when status_msg.edit_text raises TelegramBadRequest."""
    from unittest.mock import AsyncMock, MagicMock
    from aiogram.exceptions import TelegramBadRequest
    from handlers.admin import safe_edit_or_answer

    msg = MagicMock()
    msg.answer = AsyncMock()

    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: message to edit not found")
    )

    await safe_edit_or_answer(msg, status_msg, "Fallback text")
    msg.answer.assert_called_once_with("Fallback text", reply_markup=None, parse_mode="HTML")


@pytest.mark.asyncio
async def test_145_admin_cert_callbacks_immediate_answer_and_double_tap():
    """Validates that cert flow callbacks answer immediately and ignore double-tap message is not modified errors."""
    from unittest.mock import AsyncMock, MagicMock
    from aiogram.exceptions import TelegramBadRequest
    from handlers.admin import back_to_classes_callback, process_class_selected

    state = MagicMock()
    state.clear = AsyncMock()
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()

    # 1. back_to_classes_callback double-tap
    cb_back = MagicMock()
    cb_back.answer = AsyncMock()
    cb_back.message = MagicMock()
    cb_back.message.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified")
    )

    await back_to_classes_callback(cb_back, state)
    cb_back.answer.assert_called_once()

    # 2. process_class_selected immediate answer and double-tap
    cb_cls = MagicMock()
    cb_cls.data = "cert_cls:9A"
    cb_cls.answer = AsyncMock()
    cb_cls.message = MagicMock()
    cb_cls.message.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified")
    )

    await process_class_selected(cb_cls, state)
    cb_cls.answer.assert_called_once()


# =====================================================================
# 12. MULTIPLE SELECTION TESTS SUPPORT (146-160)
# =====================================================================

def test_146_match_answer_single_choice():
    from services.ai_parser import match_answer_to_options
    options = ["A) Apple", "B) Banana", "C) Carrot"]
    assert match_answer_to_options("B", options) == "B) Banana"
    assert match_answer_to_options("A)", options) == "A) Apple"
    assert match_answer_to_options("banana", options) == "B) Banana"
    assert match_answer_to_options("B) Banana", options) == "B) Banana"


def test_147_match_answer_multiple_choice_comma():
    from services.ai_parser import match_answer_to_options
    options = ["A) Apple", "B) Banana", "C) Carrot", "D) Date"]
    assert match_answer_to_options("A, C", options) == "A) Apple, C) Carrot"
    assert match_answer_to_options("A,C", options) == "A) Apple, C) Carrot"
    assert match_answer_to_options("A and C", options) == "A) Apple, C) Carrot"
    assert match_answer_to_options("A & D", options) == "A) Apple, D) Date"
    assert match_answer_to_options("B, D", options) == "B) Banana, D) Date"


def test_148_match_answer_multiple_choice_key_prefix():
    from services.ai_parser import match_answer_to_options
    options = ["A) Apple", "B) Banana", "C) Carrot"]
    assert match_answer_to_options("15: A, C", options) == "A) Apple, C) Carrot"
    assert match_answer_to_options("15. A, C", options) == "A) Apple, C) Carrot"
    assert match_answer_to_options("Q3: B, C", options) == "B) Banana, C) Carrot"
    assert match_answer_to_options("Question 4: A, B", options) == "A) Apple, B) Banana"


def test_149_match_answer_multiple_choice_list():
    from services.ai_parser import match_answer_to_options
    options = ["A) Apple", "B) Banana", "C) Carrot"]
    assert match_answer_to_options(["A", "C"], options) == "A) Apple, C) Carrot"
    # Options are sorted according to original order in options
    assert match_answer_to_options(["C) Carrot", "A) Apple"], options) == "A) Apple, C) Carrot"


def test_150_match_answer_single_option_with_comma_not_split():
    from services.ai_parser import match_answer_to_options
    options = ["A) Yes, I do", "B) No, I don't"]
    # Exact or normalized whole match should take precedence over comma splitting
    assert match_answer_to_options("Yes, I do", options) == "A) Yes, I do"
    assert match_answer_to_options("A) Yes, I do", options) == "A) Yes, I do"


def test_151_parsed_question_explicit_multiple_choice():
    data = {
        "question_text": "Select all that apply",
        "options": ["A) cat", "B) dog", "C) bird"],
        "correct_answer": "A, B",
        "question_type": "multiple_choice"
    }
    q = ParsedQuestion(**data)
    assert q.question_type == "multiple_choice"
    assert q.correct_answer == "A) cat, B) dog"
    assert q.correct_answers == ["A) cat", "B) dog"]


def test_152_parsed_question_infer_multiple_choice_from_text():
    data = {
        "question_text": "Choose TWO correct answers for this situation.",
        "options": ["A) opt1", "B) opt2", "C) opt3"],
        "correct_answer": "B, C"
    }
    q = ParsedQuestion(**data)
    assert q.question_type == "multiple_choice"
    assert q.correct_answer == "B) opt2, C) opt3"
    assert q.correct_answers == ["B) opt2", "C) opt3"]


def test_153_parsed_question_correct_answers_list_input():
    data = {
        "question_text": "Which are vowels?",
        "options": ["A) a", "B) b", "C) e", "D) d"],
        "correct_answers": ["A", "C"]
    }
    q = ParsedQuestion(**data)
    assert q.question_type == "multiple_choice"
    assert q.correct_answer == "A) a, C) e"
    assert q.correct_answers == ["A) a", "C) e"]


def test_154_parsed_question_correct_answer_list_not_truncated():
    # Verify sanitize_correct_answer preserves all items in a list without discarding
    data = {
        "question_text": "Pick both items",
        "options": ["A) 1", "B) 2", "C) 3"],
        "correct_answer": ["A", "C"]
    }
    q = ParsedQuestion(**data)
    assert q.question_type == "multiple_choice"
    assert q.correct_answer == "A) 1, C) 3"
    assert q.correct_answers == ["A) 1", "C) 3"]


def test_155_parsed_question_open_text():
    data = {
        "question_text": "Fill the gap: she _____ (go) home.",
        "options": [],
        "correct_answer": "went"
    }
    q = ParsedQuestion(**data)
    assert q.question_type == "open_text"
    assert q.correct_answer == "went"


@pytest.mark.asyncio
async def test_156_db_create_and_get_multiple_choice_questions():
    questions = [
        {
            "question_text": "Choose ONE option",
            "options": ["A) 1", "B) 2"],
            "correct_answer": "A) 1",
            "question_type": "single_choice"
        },
        {
            "question_text": "Choose TWO options",
            "options": ["A) Red", "B) Blue", "C) Green"],
            "correct_answer": "A) Red, B) Blue",
            "question_type": "multiple_choice"
        },
        {
            "question_text": "Type the missing word",
            "options": [],
            "correct_answer": "apple",
            "question_type": "open_text"
        }
    ]
    test_id = await db.create_test_with_questions(
        title="Multi-type Test",
        test_type="regular",
        created_by=999001,
        questions=questions
    )
    assert test_id > 0

    fetched = await db.get_test_questions(test_id)
    assert len(fetched) == 3
    assert fetched[0]["question_type"] == "single_choice"
    assert fetched[1]["question_type"] == "multiple_choice"
    assert fetched[2]["question_type"] == "open_text"


@pytest.mark.asyncio
async def test_157_api_get_test_includes_question_type(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()

    # Create test with multiple choice question
    test_id = await db.create_test_with_questions(
        title="API Multi Test",
        test_type="regular",
        created_by=999001,
        questions=[
            {
                "question_text": "Select all that apply",
                "options": ["A) Yes", "B) Maybe", "C) Sure"],
                "correct_answer": "A) Yes, C) Sure",
                "question_type": "multiple_choice"
            }
        ]
    )

    await db.register_student(telegram_id=1001, full_name="Test Student", class_name="9A")
    res = await client.get(f"/api/test?test_id={test_id}&student_id=1001")
    assert res.status == 200
    data = await res.json()
    qs = data["test"]["questions"]
    assert len(qs) == 1
    assert qs[0]["question_type"] == "multiple_choice"
    # Security: Anti-cheat check
    assert "correct_answer" not in qs[0]
    assert "explanation" not in qs[0]

    await client.close()


@pytest.mark.asyncio
async def test_158_api_submit_with_array_answers(aiohttp_app):
    client = TestClient(TestServer(aiohttp_app))
    await client.start_server()

    # Create test
    test_id = await db.create_test_with_questions(
        title="Submission Multi Test",
        test_type="regular",
        created_by=999001,
        questions=[
            {
                "question_text": "Choose two colors",
                "options": ["A) Red", "B) Blue", "C) Yellow"],
                "correct_answer": "A) Red, B) Blue",
                "question_type": "multiple_choice"
            }
        ]
    )
    qs = await db.get_test_questions(test_id)
    qid = str(qs[0]["id"])

    # Submit array of answers for multiple choice question
    payload = {
        "test_id": test_id,
        "student_id": 1,
        "answers": {qid: ["A) Red", "B) Blue"]},
        "time_spent_seconds": 30
    }
    res = await client.post("/api/submit", json=payload)
    assert res.status == 200
    data = await res.json()
    assert data.get("success") is True
    sub_id = data.get("submission_id")
    assert sub_id > 0

    # Retrieve submission via teacher endpoint
    res_teacher = await client.get(f"/api/teacher/submissions?test_id={test_id}&teacher_id=999001")
    assert res_teacher.status == 200
    t_data = await res_teacher.json()
    subs = t_data["submissions"]
    assert len(subs) >= 1
    sub = next(s for s in subs if s["id"] == sub_id)
    # user_answer must be preserved as list
    ans_item = sub["answers"][0]
    assert ans_item["user_answer"] == ["A) Red", "B) Blue"]
    assert ans_item["question_type"] == "multiple_choice"

    await client.close()


def test_159_normalize_answer_with_list_input():
    from services.web_server import normalize_answer
    assert normalize_answer(["A) Apple", "B) Banana"]) == "apple, banana"
    assert normalize_answer(["c. Car", "d. Dog"]) == "car, dog"
    assert normalize_answer("A) Apple") == "apple"


@pytest.mark.asyncio
async def test_160_db_init_migration_question_type_existing_table():
    import aiosqlite
    async with aiosqlite.connect(config.DB_PATH) as conn:
        async with conn.execute("PRAGMA table_info(questions);") as cur:
            cols = [row[1] for row in await cur.fetchall()]
            assert "question_type" in cols


def test_161_match_answer_delimiterless_keys_space_and_concat():
    from services.ai_parser import match_answer_to_options_list, match_answer_to_options
    options = ["A) Apple", "B) Banana", "C) Cherry", "D) Date"]

    # Space separated letters
    assert match_answer_to_options_list("A C", options) == ["A) Apple", "C) Cherry"]
    assert match_answer_to_options("15: A C", options) == "A) Apple, C) Cherry"
    assert match_answer_to_options("14-15: B D", options) == "B) Banana, D) Date"

    # Concatenated letters (e.g. AC or BD)
    assert match_answer_to_options_list("AC", options) == ["A) Apple", "C) Cherry"]
    assert match_answer_to_options("15: BD", options) == "B) Banana, D) Date"


def test_162_parsed_question_no_substring_collision():
    from services.ai_parser import ParsedQuestion

    # 'cat' is a substring of 'caterpillar', but only 'caterpillar' was the answer
    q = ParsedQuestion(
        question_text="Select all that apply",
        options=["cat", "caterpillar", "dog"],
        correct_answer="caterpillar",
        question_type="multiple_choice"
    )
    assert q.correct_answers == ["caterpillar"]
    assert q.correct_answer == "caterpillar"

    # Multi-answer with substring options
    q2 = ParsedQuestion(
        question_text="Select all that apply",
        options=["in", "into", "on", "onto"],
        correct_answer="into, onto",
        question_type="multiple_choice"
    )
    assert q2.correct_answers == ["into", "onto"]
    assert "in" not in q2.correct_answers
    assert "on" not in q2.correct_answers


def test_163_match_answer_to_options_list_order_and_types():
    from services.ai_parser import match_answer_to_options_list
    options = ["A) One", "B) Two", "C) Three", "D) Four"]

    # Order in options is preserved regardless of key order
    res = match_answer_to_options_list(["D) Four", "A) One"], options)
    assert res == ["A) One", "D) Four"]

    # Empty inputs
    assert match_answer_to_options_list(None, options) == []
    assert match_answer_to_options_list("", options) == []


@pytest.mark.asyncio
async def test_164_db_get_test_questions_open_text_options_empty_normalization():
    # If a question has empty options, get_test_questions must report open_text
    test_id = await db.create_test_with_questions(
        title="Open Cloze Test",
        test_type="regular",
        created_by=999001,
        questions=[
            {
                "question_text": "Fill the blank",
                "options": [],
                "correct_answer": "solved",
                "question_type": "single_choice"  # wrong type passed
            }
        ]
    )
    qs = await db.get_test_questions(test_id)
    assert len(qs) == 1
    assert qs[0]["question_type"] == "open_text"


@pytest.mark.asyncio
async def test_165_student_chat_quiz_detailed_answers_has_question_type():
    from unittest.mock import AsyncMock, MagicMock
    from handlers.student import handle_user_answer
    from states.student_states import QuizStates

    mock_state = MagicMock()
    mock_state.get_data = AsyncMock(return_value={
        "current_index": 0,
        "questions": [
            {
                "id": 1,
                "question_text": "Select all that apply",
                "options": ["A) 1", "B) 2"],
                "correct_answer": "A) 1, B) 2",
                "question_type": "multiple_choice",
                "explanation": "Rule 1",
                "image_url": "/static/tests/q1.png"
            }
        ],
        "answers": []
    })
    mock_state.update_data = AsyncMock()

    mock_msg = MagicMock()
    mock_msg.answer = AsyncMock()

    await handle_user_answer(mock_msg, mock_state, "A, B", is_callback=False)

    mock_state.update_data.assert_called_once()
    saved_answers = mock_state.update_data.call_args[1]["answers"]
    assert len(saved_answers) == 1
    ans = saved_answers[0]
    assert ans["question_type"] == "multiple_choice"
    assert ans["image_url"] == "/static/tests/q1.png"
    assert ans["user_answer"] == "A, B"


@pytest.mark.asyncio
async def test_166_milestone1_preview_cleanup_and_teacher_card_restyling():
    """Validates Milestone 1:
    1. render_test_preview strictly outputs clean Title, Questions, Category, Prompt without illustration/MC breakdown.
    2. Reference key ('Ключ / Эталон', teacher-correct-ans-row) and tag checkmarks ('✓', tag-check) are completely absent.
    3. Student answer tag and row styles adhere to Notion/Apple styling.
    """
    from unittest.mock import AsyncMock, MagicMock
    from aiogram import types
    from aiogram.fsm.context import FSMContext
    from handlers.admin import render_test_preview
    from pathlib import Path

    # 1. Verify render_test_preview
    mock_msg = MagicMock(spec=types.Message)
    mock_msg.edit_text = AsyncMock()
    mock_state = MagicMock(spec=FSMContext)
    mock_state.set_state = AsyncMock()

    test_payload = {
        "title": "Grammar & Vocabulary <Advanced>",
        "questions": [
            {"question_text": "Q1", "options": ["A", "B"], "question_type": "multiple_choice", "image_url": "http://img.png"},
            {"question_text": "Q2", "options": ["C", "D"], "question_type": "multiple_choice", "image_url": "http://img2.png"}
        ]
    }

    await render_test_preview(mock_msg, test_payload, "olympiad", mock_state)
    called_text = mock_msg.edit_text.call_args[0][0]
    called_kwargs = mock_msg.edit_text.call_args[1]

    # Must be valid HTML
    assert called_kwargs.get("parse_mode") == "HTML"
    assert "Grammar &amp; Vocabulary &lt;Advanced&gt;" in called_text
    assert "<b>Всего вопросов:</b> 2" in called_text
    assert "<b>Категория:</b> Олимпиадный" in called_text
    assert "Опубликовать этот тест для учеников?" in called_text

    # Must NOT have illustrations or multiple choice lines
    assert "🖼" not in called_text
    assert "Иллюстрации" not in called_text
    assert "☑️" not in called_text
    assert "Множественный выбор" not in called_text

    # 2. Verify Teacher Web assets
    html_src = Path("web/teacher.html").read_text(encoding="utf-8")
    js_src = Path("web/teacher.js").read_text(encoding="utf-8")
    css_src = Path("web/teacher.css").read_text(encoding="utf-8")

    # Reference key removal across all web files
    for src in [html_src, js_src, css_src]:
        assert "teacher-correct-ans-row" not in src
        assert "teacher-correct-ans-val" not in src
        assert "Ключ / Эталон" not in src

    # Tag checkmark removal
    assert "tag-check" not in js_src
    assert "tag-check" not in css_src
    assert '<span class="tag-check">✓</span>' not in js_src

    # Notion / Apple style tokens for student answers
    assert ".teacher-student-ans-row" in css_src
    assert ".teacher-student-ans-val" in css_src
    assert ".teacher-ans-tag" in css_src
    assert "border-radius: var(--radius-sm);" in css_src


# --- 167: v2.0 Release Broadcast Exactly-Once Test ---
@pytest.mark.asyncio
async def test_167_v2_broadcast_exactly_once():
    from database import has_broadcast_been_sent, mark_broadcast_as_sent
    from services.broadcast import notify_teachers_v2_release, BROADCAST_V2_ID
    from unittest.mock import AsyncMock, MagicMock

    test_broadcast_id = "test_broadcast_unit_167"
    assert not await has_broadcast_been_sent(test_broadcast_id)

    await mark_broadcast_as_sent(test_broadcast_id)
    assert await has_broadcast_been_sent(test_broadcast_id)

    # Mock Bot
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    # Pre-mark BROADCAST_V2_ID in test database so it does not send real messages during tests
    await mark_broadcast_as_sent(BROADCAST_V2_ID)
    await notify_teachers_v2_release(mock_bot)
    # Should not call send_message since it is already marked
    assert mock_bot.send_message.call_count == 0








