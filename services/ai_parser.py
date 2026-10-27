import json
import re
import logging
from typing import List, Optional
from pydantic import BaseModel, Field
from groq import AsyncGroq
from config import GROQ_API_KEY, GROQ_MODEL, GEMINI_API_KEY, GEMINI_MODEL

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
Analyze the provided test or olympiad materials in English and structure them into a formal test schema.

Rules:
1. Preserve original English questions accurately.
2. If there is a reading passage (Reading Comprehension) that applies to multiple questions, place the passage text in `context_text` for those questions.
3. If multiple-choice options (A, B, C, D) exist, extract them into the `options` array (formatted as 'A) ...', 'B) ...', etc.).
4. If it is an open cloze / word formation task (fill in the blank without multiple choice), leave `options` as an empty list [].
5. Identify the correct answer: look for asterisks (*), pluses (+), bold text, or answer keys at the bottom/end of the materials. If no keys are provided, determine the correct answer yourself.
6. Provide a concise explanation for the correct answer where appropriate.
7. Generate a concise, suitable title for the test.

CRITICAL: Output MUST be a valid JSON object matching this exact structure:
{
  "title": "string",
  "description": "string",
  "questions": [
    {
      "question_text": "string",
      "context_text": "string or null",
      "options": ["string"],
      "correct_answer": "string",
      "explanation": "string or null"
    }
  ]
}
Return ONLY the raw JSON object, without any additional explanations outside the JSON.
"""

def clean_json_string(text: str) -> str:
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text

def get_groq_client() -> AsyncGroq:
    return AsyncGroq(api_key=GROQ_API_KEY)

async def parse_test_content(
    text_content: str,
    test_type: str = "regular"
) -> ParsedTest:
    """
    Parses test content using Gemini API (with Groq API as fallback).
    Takes extracted text from PDF, Word (.docx), or plain text.
    """
    if not text_content or not text_content.strip():
        raise ValueError("Содержимое теста пусто или не удалось извлечь текст.")

    # Primary: Gemini API
    if GEMINI_API_KEY:
        try:
            from google import genai
            from google.genai import types
            logger.info("Attempting test parsing with Gemini model: %s", GEMINI_MODEL)
            gemini_client = genai.Client(api_key=GEMINI_API_KEY)
            contents = [
                f"Тип теста: {test_type}. {PROMPT_SYSTEM}",
                f"Материалы теста:\n\n{text_content}"
            ]
            response = await gemini_client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ParsedTest,
                    temperature=0.2,
                )
            )
            if response.parsed:
                logger.info("Successfully parsed test with Gemini (%s questions)", len(response.parsed.questions))
                return response.parsed
            data = json.loads(response.text)
            parsed = ParsedTest(**data)
            logger.info("Successfully parsed test with Gemini (%s questions)", len(parsed.questions))
            return parsed
        except Exception as e:
            logger.warning("Gemini API failed: %s. Falling back to Groq...", e)

    # Fallback: Groq API
    if GROQ_API_KEY:
        client = get_groq_client()
        models_to_try = [
            GROQ_MODEL,
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "qwen/qwen3.8-27b",
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant"
        ]
        # deduplicate preserving order
        models_to_try = list(dict.fromkeys([m for m in models_to_try if m]))

        user_prompt = f"Категория теста: {test_type}.\nРаспарси материалы в формате JSON:\n\n{text_content}"

        last_error = None
        for model_name in models_to_try:
            try:
                logger.info("Attempting test parsing with Groq model: %s", model_name)
                completion = await client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": PROMPT_SYSTEM},
                        {"role": "user", "content": user_prompt}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.2,
                )
                raw_response = completion.choices[0].message.content
                if not raw_response:
                    continue

                cleaned_json = clean_json_string(raw_response)
                data = json.loads(cleaned_json)
                parsed = ParsedTest(**data)
                logger.info("Successfully parsed test with Groq (%s questions)", len(parsed.questions))
                return parsed
            except Exception as e:
                logger.warning("Groq model %s failed: %s. Trying next...", model_name, e)
                last_error = e

        logger.error("All Groq models failed. Last error: %s", last_error)
        raise RuntimeError(f"Не удалось распарсить тест через Groq API: {last_error}")

    raise ValueError("Ни GEMINI_API_KEY, ни GROQ_API_KEY не заданы в .env!")
