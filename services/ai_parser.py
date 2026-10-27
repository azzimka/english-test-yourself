import json
import logging
from typing import List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

class ParsedQuestion(BaseModel):
    question_text: str = Field(description="Текст вопроса или задание с пропуском")
    context_text: Optional[str] = Field(default=None, description="Текст для чтения, если несколько вопросов относятся к одному рассказу/статье")
    options: List[str] = Field(default_factory=list, description="Список вариантов ответа (например, ['A) apple', 'B) banana']). Если вопрос с открытым вводом слова, список пуст.")
    correct_answer: str = Field(description="Правильный вариант ответа или слово. Если правильный ответ был отмечен галочкой/звездочкой/ключом в конце, извлеки его. Иначе реши сам.")
    explanation: Optional[str] = Field(default=None, description="Краткое пояснение грамматического или лексического правила")

class ParsedTest(BaseModel):
    title: str = Field(description="Понятное название теста (например, 'Past Tenses Quiz' или 'Олимпиада 9 класс — Reading & Grammar')")
    description: Optional[str] = Field(default="", description="Краткое описание темы теста")
    questions: List[ParsedQuestion] = Field(description="Список всех распознанных вопросов")


PROMPT_SYSTEM = """
You are an expert English teacher and exam specialist.
Analyze the provided test or olympiad materials (which may be a Word document, raw text, or a photo/scan of a worksheet).
Carefully parse all tasks into individual questions.

Rules:
1. Preserve the original English questions accurately.
2. If there is a reading passage (Reading Comprehension) that applies to multiple questions, place the passage text in `context_text` for those questions.
3. If options (A, B, C, D) exist, extract them into `options`.
4. If it is an open cloze / word formation task (fill in the blank without multiple choice), leave `options` empty.
5. Identify the correct answer: look for asterisks (*), pluses (+), bold text, or answer keys at the bottom/end of the file. If no keys are provided, determine the correct answer yourself.
6. Provide a concise explanation for the correct answer where appropriate.
7. Generate a concise, suitable title for the test.
"""

def get_genai_client() -> genai.Client:
    return genai.Client(api_key=GEMINI_API_KEY)

async def parse_test_content(
    text_content: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    mime_type: Optional[str] = None,
    test_type: str = "regular"
) -> ParsedTest:
    """
    Parses test content (text, Word .docx, PDF, or image) using Gemini API with automatic fallback.
    """
    client = get_genai_client()
    
    contents = [f"Тип теста: {test_type}. {PROMPT_SYSTEM}"]
    
    if text_content:
        contents.append(f"Материалы теста:\n\n{text_content}")
    elif file_bytes and mime_type:
        part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
        contents.append(part)
        contents.append("Распознай и распарси все задания теста из этого документа/изображения.")
    else:
        raise ValueError("Either text_content or (file_bytes and mime_type) must be provided.")

    # Try primary model, then fallback if busy
    models_to_try = [GEMINI_MODEL, "gemini-3.8-flash", "gemini-flash-latest"]
    # remove duplicates while preserving order
    models_to_try = list(dict.fromkeys(models_to_try))

    last_error = None
    for model_name in models_to_try:
        try:
            logger.info("Attempting test parsing with model: %s", model_name)
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ParsedTest,
                    temperature=0.2,
                )
            )
            # When response_schema is provided, response.parsed contains the Pydantic instance
            if response.parsed:
                return response.parsed
            
            # Fallback if raw text returned
            data = json.loads(response.text)
            return ParsedTest(**data)
            
        except Exception as e:
            logger.warning("Model %s failed with error: %s. Trying next model...", model_name, e)
            last_error = e

    raise RuntimeError(f"All Gemini models failed to parse the test. Last error: {last_error}")
