from typing import List, Dict, Any, Optional
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import WEBAPP_URL

def get_student_main_kb() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="📚 Обычные тесты"), KeyboardButton(text="🏆 Олимпиадные тесты")],
        [KeyboardButton(text="📈 Мои результаты")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_student_tests_kb(tests: List[Dict[str, Any]], completed_map: Dict[int, Dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons = []
    for t in tests:
        test_id = t["id"]
        title = t.get("title", "Тест")
        
        if test_id in completed_map:
            btn_text = f"✅ {title[:30]} (Сдано)"
        else:
            btn_text = f"🆕 {title[:35]}"

        buttons.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"select_test:{test_id}")
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_start_test_confirm_kb(test_id: int, already_passed: bool, is_olympiad: bool, student_tg_id: int) -> InlineKeyboardMarkup:
    if already_passed and is_olympiad:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_catalog")]
        ])
    
    start_label = "🔄 Пройти снова" if already_passed else "🚀 Открыть тест (Mini App)"
    buttons = []
    if WEBAPP_URL:
        webapp_url = f"{WEBAPP_URL}/?test_id={test_id}&student_id={student_tg_id}"
        buttons.append([InlineKeyboardButton(text=start_label, web_app=WebAppInfo(url=webapp_url))])
    else:
        buttons.append([InlineKeyboardButton(text=start_label, callback_data=f"begin_quiz:{test_id}")])

    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_catalog")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_question_options_kb(options: List[str]) -> InlineKeyboardMarkup:
    buttons = []
    for idx, opt in enumerate(options):
        buttons.append([
            InlineKeyboardButton(text=opt, callback_data=f"quiz_choice:{idx}")
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
