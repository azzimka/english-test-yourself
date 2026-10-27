from aiogram.fsm.state import State, StatesGroup

class RegistrationStates(StatesGroup):
    waiting_for_full_name = State()
    waiting_for_class = State()

class QuizStates(StatesGroup):
    solving = State()
