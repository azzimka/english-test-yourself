from aiogram.fsm.state import State, StatesGroup

class UploadTestStates(StatesGroup):
    waiting_for_test_type = State()
    waiting_for_title = State()
    waiting_for_content = State()
    waiting_for_confirmation = State()
    editing_title = State()
    renaming_existing_test = State()

class CertificateStates(StatesGroup):
    selecting_class = State()
    selecting_student = State()
    searching_student = State()
    waiting_for_certificate = State()
