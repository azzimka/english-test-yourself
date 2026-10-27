import html
from aiogram import Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from config import ADMIN_IDS
from database import get_user_by_telegram_id, register_student
from keyboards.admin_kb import get_admin_main_kb
from keyboards.student_kb import get_student_main_kb
from states.student_states import RegistrationStates

router = Router()

@router.message(Command("id"))
async def cmd_my_id(message: types.Message):
    await message.answer(
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n\n"
        "Скопируйте его и вставьте в файл <code>.env</code> в параметр <code>ADMIN_IDS</code>.",
        parse_mode="HTML"
    )

@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    # 1. Проверяем, является ли пользователь учителем (администратором)
    if user_id in ADMIN_IDS:
        first_name = html.escape(message.from_user.first_name or "Учитель")
        await message.answer(
            f"👩‍🏫 Здравствуйте, {first_name}!\n\n"
            "Вы вошли как <b>учитель / администратор</b> бота.\n"
            "Здесь вы можете загружать новые тесты, управлять ими и скачивать ведомости с оценками учеников.",
            reply_markup=get_admin_main_kb(),
            parse_mode="HTML"
        )
        return

    # 2. Если это ученик, проверяем регистрацию в базе данных
    user = await get_user_by_telegram_id(user_id)
    if user and user.get("full_name"):
        name = html.escape(user['full_name'])
        cls = html.escape(user['class_name'] or "")
        await message.answer(
            f"👋 Привет, {name} ({cls})!\n\n"
            "Выбирай нужный раздел в меню ниже, чтобы начать решать тесты:",
            reply_markup=get_student_main_kb(),
            parse_mode="HTML"
        )
    else:
        # Новый ученик — просим представиться
        await message.answer(
            "👋 Привет! Добро пожаловать в бот тестирования по английскому языку.\n\n"
            "Чтобы учительница видела твои результаты и оценки, представься один раз.\n\n"
            "✍️ Напиши свои <b>Фамилию и Имя</b> (например: <i>Иванов Иван</i>):",
            parse_mode="HTML"
        )
        await state.set_state(RegistrationStates.waiting_for_full_name)


# --- Сценарий регистрации ученика ---
@router.message(RegistrationStates.waiting_for_full_name)
async def process_full_name(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    if len(full_name.split()) < 2:
        await message.answer(
            "Пожалуйста, укажи и <b>Фамилию</b>, и <b>Имя</b> (через пробел):",
            parse_mode="HTML"
        )
        return
    
    await state.update_data(full_name=full_name)
    await message.answer(
        f"Отлично, {html.escape(full_name)}!\n\n"
        "🏫 Теперь укажи свой <b>класс</b> (например: <i>9-А</i> или <i>10-Б</i>):",
        parse_mode="HTML"
    )
    await state.set_state(RegistrationStates.waiting_for_class)


@router.message(RegistrationStates.waiting_for_class)
async def process_class(message: types.Message, state: FSMContext):
    class_name = message.text.strip().upper()
    data = await state.get_data()
    full_name = data.get("full_name")

    await register_student(
        telegram_id=message.from_user.id,
        full_name=full_name,
        class_name=class_name
    )
    await state.clear()

    await message.answer(
        f"✅ Регистрация завершена!\n\n"
        f"👤 Ученик: <b>{html.escape(full_name)}</b>\n"
        f"🏫 Класс: <b>{html.escape(class_name)}</b>\n\n"
        "Теперь ты можешь приступать к тестам. Выбирай раздел в меню:",
        reply_markup=get_student_main_kb(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "cancel_action")
async def process_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Действие отменено.", parse_mode="HTML")
    await callback.answer()
