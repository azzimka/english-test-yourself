from typing import List, Dict, Any
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import WEBAPP_URL

def get_admin_main_kb(teacher_id: int | None = None) -> ReplyKeyboardMarkup:
    if WEBAPP_URL and teacher_id:
        results_btn = KeyboardButton(
            text="📊 Результаты",
            web_app=WebAppInfo(url=f"{WEBAPP_URL}/teacher?teacher_id={teacher_id}")
        )
    elif WEBAPP_URL:
        results_btn = KeyboardButton(
            text="📊 Результаты",
            web_app=WebAppInfo(url=f"{WEBAPP_URL}/teacher")
        )
    else:
        results_btn = KeyboardButton(text="📊 Результаты")

    kb = [
        [KeyboardButton(text="📝 Создать тест")],
        [KeyboardButton(text="📂 Мои тесты"), results_btn],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_test_type_choice_kb() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="📚 Обычный тест", callback_data="set_test_type:regular"),
            InlineKeyboardButton(text="🏆 Олимпиадный тест", callback_data="set_test_type:olympiad"),
        ],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_title_choice_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_confirm_test_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="✅ Опубликовать тест", callback_data="confirm_publish")],
        [InlineKeyboardButton(text="✏️ Изменить название", callback_data="edit_title_btn")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_action")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_tests_kb(tests: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons = []
    for t in tests:
        type_icon = "🏆" if t.get("test_type") == "olympiad" else "📚"
        title = t.get("title", "Без названия")
        buttons.append([
            InlineKeyboardButton(
                text=f"{type_icon} {title[:35]}",
                callback_data=f"manage_test:{t['id']}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_test_actions_kb(test_id: int) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"rename_test:{test_id}")],
        [InlineKeyboardButton(text="🗑 Удалить тест", callback_data=f"delete_test:{test_id}")],
        [InlineKeyboardButton(text="⬅️ Назад ко всем тестам", callback_data="admin_tests_list")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_cancel_rename_kb(test_id: int) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"manage_test:{test_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)
