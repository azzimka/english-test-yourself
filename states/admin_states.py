from aiogram.fsm.state import State, StatesGroup

class UploadTestStates(StatesGroup):
    waiting_for_test_type = State()
    waiting_for_title = State()
    waiting_for_content = State()
    waiting_for_confirmation = State()
    editing_title = State()
    renaming_existing_test = State()
