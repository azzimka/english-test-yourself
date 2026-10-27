from typing import List, Dict, Any
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

def get_admin_main_kb() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="📝 Создать тест")],
        [KeyboardButton(text="📂 Мои тесты"), KeyboardButton(text="📊 Результаты учеников")],
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
        [InlineKeyboardButton(text="✨ Пусть ИИ определит название", callback_data="skip_title")],
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
        status_icon = "🟢" if t.get("is_active") else "🔴"
        type_icon = "🏆" if t.get("test_type") == "olympiad" else "📚"
        title = t.get("title", "Без названия")
        buttons.append([
            InlineKeyboardButton(
                text=f"{status_icon} {type_icon} {title[:35]}",
                callback_data=f"manage_test:{t['id']}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_test_actions_kb(test_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle_text = "🔴 Скрыть от учеников" if is_active else "🟢 Сделать активным"
    kb = [
        [InlineKeyboardButton(text=toggle_text, callback_data=f"toggle_active:{test_id}")],
        [InlineKeyboardButton(text="📥 Скачать Excel с результатами", callback_data=f"export_test_excel:{test_id}")],
        [InlineKeyboardButton(text="🗑 Удалить тест", callback_data=f"delete_test:{test_id}")],
        [InlineKeyboardButton(text="⬅️ Назад ко всем тестам", callback_data="admin_tests_list")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)
