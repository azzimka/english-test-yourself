"""
Adversarial empirical stress tests for Milestone 1:
1. render_test_preview adversarial inputs, XML/HTML validation, forbidden strings check.
2. Teacher Mini App card rendering, absence of deprecated keys and checkmarks.
"""

import os
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import html
import xml.etree.ElementTree as ET
import pytest
from unittest.mock import AsyncMock, MagicMock
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from handlers.admin import render_test_preview, UploadTestStates


def validate_telegram_html(text: str) -> None:
    """Strictly validates that text complies with Telegram Bot API HTML format.
    
    1. Valid XML structure when wrapped in <root>...</root>
    2. Only allowed Telegram HTML tags (b, strong, i, em, u, ins, s, strike, del, span, tg-spoiler, a, code, pre, blockquote, tg-emoji)
    3. No malformed or unescaped entities
    """
    root = ET.fromstring(f"<root>{text}</root>")
    allowed_tags = {
        "root", "b", "strong", "i", "em", "u", "ins", "s", "strike",
        "del", "span", "tg-spoiler", "a", "code", "pre", "blockquote", "tg-emoji"
    }
    for elem in root.iter():
        assert elem.tag in allowed_tags, f"Disallowed tag <{elem.tag}> in Telegram HTML: {text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("title_input", [
    "<script>alert('xss')</script>",
    "Advanced & Complex <C1/C2> \"Grammar\" 'Test'",
    "",
    None,
    "   ",
    "100% Pure Cyrillic: Олимпиадный тур по английскому языку №1",
    "Emoji test: 🇬🇧 🎯 ✨ 🚀 🌟",
    "A" * 1000,
    "<<<<<>>>>>&&&&&\"\"\"\"''''",
])
async def test_adversarial_render_test_preview_titles(title_input):
    expected_in_title = html.escape(str(title_input or "English Test"))
    mock_msg = MagicMock(spec=types.Message)
    mock_msg.edit_text = AsyncMock()
    mock_state = MagicMock(spec=FSMContext)
    mock_state.set_state = AsyncMock()

    payload = {
        "title": title_input,
        "questions": [
            {"question_text": "Q1", "options": ["A", "B"], "question_type": "multiple_choice", "image_url": "https://example.com/img1.png"},
            {"question_text": "Q2", "options": ["C", "D"], "question_type": "multiple_choice", "image_url": "https://example.com/img2.png"},
            {"question_text": "Q3", "question_type": "open_question"}
        ]
    }

    for test_type in ["olympiad", "regular", "unknown", ""]:
        mock_msg.edit_text.reset_mock()
        mock_state.set_state.reset_mock()

        await render_test_preview(mock_msg, payload, test_type, mock_state)

        assert mock_msg.edit_text.called
        called_text = mock_msg.edit_text.call_args[0][0]
        called_kwargs = mock_msg.edit_text.call_args[1]

        # Check parse_mode
        assert called_kwargs.get("parse_mode") == "HTML"

        # Check state transition
        mock_state.set_state.assert_called_once_with(UploadTestStates.waiting_for_confirmation)

        # Check title rendering
        assert expected_in_title in called_text

        # Validate Telegram HTML conformance
        validate_telegram_html(called_text)

        # Strictly assert absence of removed lines
        assert "🖼" not in called_text
        assert "Иллюстрации" not in called_text
        assert "Иллюстрации/схемы" not in called_text
        assert "☑️" not in called_text
        assert "Множественный выбор" not in called_text

        # Strictly assert presence of required lines
        assert "📌 <b>Название:</b>" in called_text
        assert "📊 <b>Всего вопросов:</b> 3" in called_text
        expected_cat = "Олимпиадный" if test_type == "olympiad" else "Обычный"
        assert f"🏷 <b>Категория:</b> {expected_cat}" in called_text
        assert "Опубликовать этот тест для учеников?" in called_text


@pytest.mark.asyncio
@pytest.mark.parametrize("questions_data, expected_count", [
    ([], 0),
    ([{"question_text": f"Q{i}", "question_type": "multiple_choice", "image_url": f"http://img{i}.png"} for i in range(50)], 50),
    ([{"question_text": f"Q{i}", "question_type": "open_question"} for i in range(30)], 30),
    ([{"question_text": f"Q{i}", "question_type": "multiple_choice"} for i in range(15)] +
     [{"question_text": f"Q{i}", "question_type": "open_question", "image_url": f"http://img{i}.png"} for i in range(15)], 30),
])
async def test_adversarial_render_test_preview_question_breakdown_never_rendered(questions_data, expected_count):
    mock_msg = MagicMock(spec=types.Message)
    mock_msg.edit_text = AsyncMock()
    mock_state = MagicMock(spec=FSMContext)
    mock_state.set_state = AsyncMock()

    payload = {
        "title": "Question Type Breakdown Test",
        "questions": questions_data
    }

    await render_test_preview(mock_msg, payload, "regular", mock_state)

    called_text = mock_msg.edit_text.call_args[0][0]

    assert f"📊 <b>Всего вопросов:</b> {expected_count}" in called_text
    assert "🖼" not in called_text
    assert "Иллюстрации/схемы" not in called_text
    assert "☑️" not in called_text
    assert "Множественный выбор" not in called_text

    validate_telegram_html(called_text)


@pytest.mark.asyncio
async def test_render_test_preview_fallback_on_edit_failure():
    """Verify fallback from edit_text to answer when TelegramBadRequest occurs."""
    mock_msg = MagicMock(spec=types.Message)
    mock_msg.edit_text = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="message to edit not found"))
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


def test_teacher_web_assets_adversarial_inspection():
    """Empirically inspects web/teacher.js, web/teacher.css, and web/teacher.html."""
    js_text = Path("web/teacher.js").read_text(encoding="utf-8")
    css_text = Path("web/teacher.css").read_text(encoding="utf-8")
    html_text = Path("web/teacher.html").read_text(encoding="utf-8")

    forbidden_tokens = [
        "teacher-correct-ans-row",
        "teacher-correct-ans-val",
        "Ключ / Эталон",
        "tag-check",
    ]

    for token in forbidden_tokens:
        assert token not in js_text, f"Forbidden token '{token}' found in web/teacher.js"
        assert token not in css_text, f"Forbidden token '{token}' found in web/teacher.css"
        assert token not in html_text, f"Forbidden token '{token}' found in web/teacher.html"

    # Verify student answer tags in teacher.js do not contain checkmark
    assert '<span class="tag-check">✓</span>' not in js_text
    assert 'class="tag-check"' not in js_text

    # Verify teacher-ans-tag has no hardcoded checkmark
    assert '<span class="teacher-ans-tag">' in js_text
    # Verify the mapping template
    expected_tag_template = 'nonEmpty.map((val) => `<span class="teacher-ans-tag">${escapeHtml(val)}</span>`).join("")'
    assert expected_tag_template in js_text
