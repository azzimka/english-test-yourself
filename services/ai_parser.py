import json
import re
import logging
import asyncio
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field
from groq import AsyncGroq
from config import GROQ_API_KEY, GROQ_MODEL, GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)


# =====================================================================
# Pydantic Models
# =====================================================================

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


# =====================================================================
# Prompts
# =====================================================================

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

CHUNK_PROMPT = """You are an expert English exam parser.
Your task is to parse a SPECIFIC BATCH of questions from an English test into JSON format.

CRITICAL RULES:
1. Parse EVERY SINGLE question present in this batch without omitting or summarizing any. Do NOT include examples (such as Example (0) or items where the answer is already filled in as a demonstration).
2. If multiple-choice options (A, B, C, D) exist, put them into `options` (formatted as 'A) ...', 'B) ...', etc.).
3. If it is an open cloze / word formation task (fill in the blank without multiple choice), leave `options` as an empty list [].
4. If an Answer Key is provided below, use it to accurately fill `correct_answer`.
5. If no answer key is provided, determine the correct answer yourself.
6. Keep `explanation` very brief (1 short sentence) or null to save tokens.
7. If questions refer to a reading passage in this batch, include that text in `context_text`.

Output MUST be a valid JSON object:
{
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
Return ONLY the raw JSON object."""


# =====================================================================
# Constants
# =====================================================================

CHUNK_SIZE = 25  # Target number of questions per chunk
MAX_SINGLE_BATCH = 25  # Max questions for single (non-chunked) request

# Fallback models for Groq API
GROQ_FALLBACK_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]

# Regex patterns
QUESTION_START_PATTERN = re.compile(
    r"(?:^|\n)\s*(\d+)\s*(?:[.):\-]\s*|\s+(?=[A-ZА-ЯЁ]))",
    re.MULTILINE
)

ANSWER_KEY_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:ANSWER\s*KEY[S]?|ANSWERS|KEYS|ОТВЕТЫ|КЛЮЧИ)\b[\s:]*(.*)$",
    re.IGNORECASE | re.DOTALL
)

READING_PASSAGE_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:Text\s+\d+|Passage\s+\d+|Read\s+the\s+(?:text|passage|following)|READING\s+(?:COMPREHENSION|PASSAGE|TEXT))",
    re.IGNORECASE | re.MULTILINE
)


# =====================================================================
# Helper Functions
# =====================================================================

def clean_json_string(text: str) -> str:
    """Remove markdown code fences and extra whitespace from JSON string."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text


def get_groq_client() -> AsyncGroq:
    """Create and return an AsyncGroq client."""
    return AsyncGroq(api_key=GROQ_API_KEY)


def extract_answer_keys(text: str) -> Tuple[str, str]:
    """
    Extract the ANSWER KEY section from the end of the test text.

    Returns:
        Tuple of (body_text_without_keys, answer_keys_text).
        If no answer key found, answer_keys_text is empty string.
    """
    match = ANSWER_KEY_PATTERN.search(text)
    if match:
        key_start = match.start()
        body_text = text[:key_start].strip()
        answer_keys_text = text[key_start:].strip()
        return body_text, answer_keys_text
    return text, ""


def _find_question_positions(text: str) -> List[Tuple[int, int]]:
    """
    Find all question start positions in the text.

    Returns:
        List of (question_number, char_position) tuples sorted by position.
    """
    positions = []
    for match in QUESTION_START_PATTERN.finditer(text):
        q_num = int(match.group(1))
        # Skip example question 0 if present
        if q_num == 0:
            continue
        pos = match.start()
        # Adjust position to start of line (skip leading newline from pattern)
        if text[pos] == '\n':
            pos += 1
        positions.append((q_num, pos))
    return positions


def _find_reading_passage_positions(text: str) -> List[int]:
    """
    Find positions of reading passage markers in the text.

    Returns:
        List of character positions where reading passages start.
    """
    positions = []
    for match in READING_PASSAGE_PATTERN.finditer(text):
        pos = match.start()
        if pos > 0 and text[pos] == '\n':
            pos += 1
        positions.append(pos)
    return positions


def split_test_into_chunks(body_text: str, chunk_size: int = CHUNK_SIZE) -> List[str]:
    """
    Split the test body text into chunks of approximately `chunk_size` questions.

    Rules:
    - Never split a question in the middle.
    - Keep reading passages together with their associated questions.
    - Target chunk_size questions per chunk.

    Returns:
        List of text chunks.
    """
    question_positions = _find_question_positions(body_text)

    if not question_positions:
        # No numbered questions found — return entire text as single chunk
        return [body_text.strip()] if body_text.strip() else []

    total_questions = len(question_positions)

    if total_questions <= chunk_size:
        return [body_text.strip()]

    reading_positions = _find_reading_passage_positions(body_text)

    chunks = []
    current_chunk_start_idx = 0

    while current_chunk_start_idx < total_questions:
        # Calculate the ideal end index for this chunk
        ideal_end_idx = min(current_chunk_start_idx + chunk_size, total_questions)

        prev_q_pos = (
            question_positions[current_chunk_start_idx - 1][1]
            if current_chunk_start_idx > 0 else 0
        )

        if ideal_end_idx >= total_questions:
            # Last chunk: take everything remaining
            chunk_text_start = question_positions[current_chunk_start_idx][1]

            # Also include any reading passage text immediately before this chunk's first question
            passage_start = _find_passage_before_question(
                body_text, chunk_text_start, reading_positions, prev_q_pos
            )
            if passage_start is not None and passage_start < chunk_text_start:
                chunk_text_start = passage_start

            chunk_text = body_text[chunk_text_start:].strip()
            if chunk_text:
                chunks.append(chunk_text)
            break

        # Check if the boundary falls inside a reading passage group
        # Extend the chunk to include all questions tied to the same passage
        actual_end_idx = _adjust_boundary_for_reading(
            body_text, question_positions, ideal_end_idx, reading_positions
        )

        # Extract chunk text
        chunk_text_start = question_positions[current_chunk_start_idx][1]

        # Include any reading passage immediately before first question
        passage_start = _find_passage_before_question(
            body_text, chunk_text_start, reading_positions, prev_q_pos
        )
        if passage_start is not None and passage_start < chunk_text_start:
            chunk_text_start = passage_start

        if actual_end_idx < total_questions:
            chunk_text_end = question_positions[actual_end_idx][1]
            # Check for reading passage immediately before the next chunk's first question
            prev_for_next = question_positions[actual_end_idx - 1][1]
            passage_before_next = _find_passage_before_question(
                body_text, chunk_text_end, reading_positions, prev_for_next
            )
            if passage_before_next is not None and passage_before_next < chunk_text_end:
                chunk_text_end = passage_before_next
        else:
            chunk_text_end = len(body_text)

        chunk_text = body_text[chunk_text_start:chunk_text_end].strip()
        if chunk_text:
            chunks.append(chunk_text)

        current_chunk_start_idx = actual_end_idx

    return chunks


def _find_passage_before_question(
    text: str,
    question_pos: int,
    reading_positions: List[int],
    prev_question_pos: int = 0
) -> Optional[int]:
    """
    Find a reading passage that appears immediately before the given question position
    (i.e., between prev_question_pos and question_pos, with no intervening questions).
    """
    for rp in reading_positions:
        if prev_question_pos <= rp < question_pos:
            return rp
    return None


def _adjust_boundary_for_reading(
    text: str,
    question_positions: List[Tuple[int, int]],
    boundary_idx: int,
    reading_positions: List[int]
) -> int:
    """
    Adjust the chunk boundary to avoid splitting reading passage groups.

    If the boundary falls between a reading passage start and the end of
    questions referring to that passage, extend the boundary to include
    all those questions.
    """
    if boundary_idx >= len(question_positions):
        return boundary_idx

    boundary_pos = question_positions[boundary_idx][1]

    # Find the nearest reading passage before the boundary
    relevant_passage = None
    for rp in reading_positions:
        if rp < boundary_pos:
            relevant_passage = rp

    if relevant_passage is None:
        return boundary_idx

    # Find the first question that follows the reading passage
    passage_first_q_idx = None
    for i, (_, pos) in enumerate(question_positions):
        if pos > relevant_passage:
            passage_first_q_idx = i
            break

    if passage_first_q_idx is None:
        return boundary_idx

    # Only adjust if the passage started recently (within 10 questions before boundary)
    if passage_first_q_idx < boundary_idx and (boundary_idx - passage_first_q_idx <= 10):
        next_passage_pos = None
        for rp in reading_positions:
            if rp > boundary_pos:
                next_passage_pos = rp
                break

        if next_passage_pos is not None:
            # Find which question index corresponds to the next passage
            for i, (_, pos) in enumerate(question_positions):
                if pos >= next_passage_pos:
                    if i - boundary_idx <= 10:
                        return i
                    break

    return boundary_idx


def count_questions_in_text(text: str) -> int:
    """Count the number of questions detected in the text by pattern matching."""
    return len(_find_question_positions(text))


# =====================================================================
# Groq API Calls
# =====================================================================

async def _call_groq_with_fallback(
    client: AsyncGroq,
    messages: List[dict],
    models: List[str],
    max_tokens: int = 8192,
    temperature: float = 0.1,
) -> str:
    """
    Call Groq API with model fallback chain.

    Returns raw response text.
    Raises RuntimeError if all models fail.
    """
    last_error = None
    for model_name in models:
        try:
            logger.info("Calling Groq model: %s", model_name)
            completion = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=temperature,
                max_tokens=max_tokens,
            )
            raw = completion.choices[0].message.content
            if raw:
                return raw
        except Exception as e:
            logger.warning("Groq model %s failed: %s. Trying next...", model_name, e)
            last_error = e
            # Brief pause before retry on rate-limit
            if "429" in str(e) or "rate" in str(e).lower():
                await asyncio.sleep(2)

    raise RuntimeError(f"All Groq models failed. Last error: {last_error}")


async def parse_single_chunk(
    client: AsyncGroq,
    chunk_text: str,
    answer_keys_text: str,
    models: List[str],
) -> List[dict]:
    """
    Parse a single chunk of questions through Groq API.

    Returns list of question dicts.
    """
    user_content_parts = [f"Parse ALL questions from this batch:\n\n{chunk_text}"]
    if answer_keys_text:
        user_content_parts.append(
            f"\n\nANSWER KEY REFERENCE (Use ONLY to look up the correct_answer for the questions in THIS batch. Do NOT generate questions from this key):\n{answer_keys_text}"
        )

    user_content = "".join(user_content_parts)

    messages = [
        {"role": "system", "content": CHUNK_PROMPT},
        {"role": "user", "content": user_content},
    ]

    raw_response = await _call_groq_with_fallback(
        client, messages, models, max_tokens=8192, temperature=0.1
    )

    cleaned = clean_json_string(raw_response)
    data = json.loads(cleaned)

    questions = data.get("questions", [])
    if not questions and isinstance(data, list):
        questions = data

    return questions


# =====================================================================
# Main Entry Point
# =====================================================================

async def parse_test_content(
    text_content: str,
    test_type: str = "regular"
) -> ParsedTest:
    """
    Parses test content using Groq API with intelligent chunking for large tests.
    Falls back to Gemini API if configured and Groq fails.

    For tests with <= 25 questions: single request (fast path).
    For tests with > 25 questions: parallel chunked processing via asyncio.gather.
    """
    if not text_content or not text_content.strip():
        raise ValueError("Содержимое теста пусто или не удалось извлечь текст.")

    # --- Step 1: Extract answer keys from the end of the document ---
    body_text, answer_keys_text = extract_answer_keys(text_content)
    if answer_keys_text:
        logger.info("Answer key section found and extracted (%d chars)", len(answer_keys_text))

    # --- Step 2: Count questions and decide strategy ---
    question_count = count_questions_in_text(body_text)
    logger.info("Detected approximately %d questions in the test", question_count)

    # Build the model fallback chain
    models = _build_model_chain()

    # --- Fast path: small test (<= 25 questions) → single request ---
    if question_count <= MAX_SINGLE_BATCH:
        return await _parse_single_request(text_content, test_type, models)

    # --- Chunked path: large test (> 25 questions) → parallel processing ---
    logger.info("Large test detected (%d questions). Using chunked parallel processing.", question_count)
    return await _parse_chunked(body_text, answer_keys_text, test_type, models)


def _build_model_chain() -> List[str]:
    """Build deduplicated model fallback chain."""
    models = [
        GROQ_MODEL,
        *GROQ_FALLBACK_MODELS,
    ]
    # Deduplicate preserving order
    seen = set()
    result = []
    for m in models:
        if m and m not in seen:
            seen.add(m)
            result.append(m)
    return result


async def _parse_single_request(
    text_content: str,
    test_type: str,
    models: List[str],
) -> ParsedTest:
    """
    Parse a small test (≤ 25 questions) in a single Groq API request.
    Falls back to Gemini if Groq fails.
    """
    # Primary: Groq single request
    if GROQ_API_KEY:
        try:
            client = get_groq_client()
            user_prompt = f"Категория теста: {test_type}.\nРаспарси материалы в формате JSON:\n\n{text_content}"

            messages = [
                {"role": "system", "content": PROMPT_SYSTEM},
                {"role": "user", "content": user_prompt},
            ]

            raw_response = await _call_groq_with_fallback(
                client, messages, models, max_tokens=8192, temperature=0.1
            )

            cleaned = clean_json_string(raw_response)
            data = json.loads(cleaned)
            parsed = ParsedTest(**data)
            logger.info("Successfully parsed test in single request (%d questions)", len(parsed.questions))
            return parsed
        except Exception as e:
            logger.warning("Groq single request failed: %s. Falling back to Gemini...", e)

    # Fallback to Gemini if Groq fails or is not configured
    if GEMINI_API_KEY:
        try:
            return await _parse_with_gemini(text_content, test_type)
        except Exception as e:
            logger.error("Gemini fallback also failed: %s", e)

    raise ValueError("Ни Groq API, ни Gemini API не смогли распарсить тест.")


async def _parse_chunked(
    body_text: str,
    answer_keys_text: str,
    test_type: str,
    models: List[str],
) -> ParsedTest:
    """
    Parse a large test using parallel chunked processing.

    1. Split text into chunks of ~25 questions.
    2. Extract title from the first ~500 chars via a micro-request.
    3. Parse all chunks in parallel via asyncio.gather.
    4. Merge results and verify completeness.
    """
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY не задан в .env! Для чанкинга необходим Groq API.")

    client = get_groq_client()

    # --- Split into chunks ---
    chunks = split_test_into_chunks(body_text, chunk_size=CHUNK_SIZE)
    logger.info("Split test into %d chunks for parallel processing", len(chunks))

    # --- Extract title (quick micro-request from first 500 chars) ---
    title_task = _extract_title(client, body_text[:500], test_type, models)

    # --- Parse all chunks in parallel ---
    chunk_tasks = [
        parse_single_chunk(client, chunk, answer_keys_text, models)
        for chunk in chunks
    ]

    # Run title extraction and all chunk parsing in parallel
    results = await asyncio.gather(title_task, *chunk_tasks, return_exceptions=True)

    # --- Process results ---
    title_result = results[0]
    chunk_results = results[1:]

    # Extract title
    if isinstance(title_result, Exception):
        logger.warning("Title extraction failed: %s. Using fallback.", title_result)
        title = f"English Test ({test_type})"
        description = ""
    else:
        title, description = title_result

    # --- Merge all questions ---
    all_questions = []
    for i, result in enumerate(chunk_results):
        if isinstance(result, Exception):
            logger.error("Chunk %d failed: %s", i + 1, result)
            raise RuntimeError(f"Не удалось обработать часть {i+1} теста: {result}")
        all_questions.extend(result)

    if not all_questions:
        raise RuntimeError("Не удалось извлечь ни одного вопроса из теста.")

    # Deduplicate questions if any chunk overlap occurred
    deduped_questions = []
    seen = set()
    for q in all_questions:
        q_text_norm = re.sub(r"\s+", " ", q.get("question_text", "").strip().lower())
        opts_norm = tuple(re.sub(r"\s+", " ", opt.strip().lower()) for opt in q.get("options", []))
        key = (q_text_norm, opts_norm)
        if key in seen and q_text_norm:
            logger.warning("Dropped duplicate question from chunk overlap: %s", q.get("question_text", "")[:50])
            continue
        seen.add(key)
        deduped_questions.append(q)
    all_questions = deduped_questions

    # --- Build ParsedTest ---
    parsed_questions = [ParsedQuestion(**q) for q in all_questions]
    parsed = ParsedTest(
        title=title,
        description=description or "",
        questions=parsed_questions,
    )

    logger.info(
        "Successfully parsed chunked test: '%s' with %d questions from %d chunks",
        title, len(parsed_questions), len(chunks)
    )

    # --- Verify numbering completeness ---
    _verify_question_completeness(parsed_questions, body_text)

    return parsed


async def _extract_title(
    client: AsyncGroq,
    header_text: str,
    test_type: str,
    models: List[str],
) -> Tuple[str, str]:
    """
    Extract test title and description from the first ~500 chars of the test.

    Returns (title, description) tuple.
    """
    prompt = (
        "Extract ONLY the title and a brief description from this test header. "
        f"Test category: {test_type}.\n\n"
        "Output JSON: {\"title\": \"string\", \"description\": \"string\"}\n\n"
        f"Header text:\n{header_text}"
    )

    messages = [
        {"role": "system", "content": "You extract test titles. Return only JSON."},
        {"role": "user", "content": prompt},
    ]

    raw = await _call_groq_with_fallback(
        client, messages, models, max_tokens=256, temperature=0.1
    )
    cleaned = clean_json_string(raw)
    data = json.loads(cleaned)
    raw_title = data.get("title", "English Test")
    cleaned_title = re.sub(r"\s*-\s*Page\s+\d+.*$", "", raw_title, flags=re.IGNORECASE).strip()
    return cleaned_title or raw_title, data.get("description", "")


def _verify_question_completeness(questions: List[ParsedQuestion], body_text: str):
    """
    Log a warning if the parsed question count doesn't match
    the number of questions detected in the original text.
    """
    expected = count_questions_in_text(body_text)
    actual = len(questions)

    if expected > 0 and actual < expected:
        logger.warning(
            "Question count mismatch! Expected ~%d questions but parsed %d. "
            "Some questions may have been lost.",
            expected, actual
        )
    elif actual >= expected:
        logger.info(
            "Question completeness verified: parsed %d questions (expected ~%d)",
            actual, expected
        )


# =====================================================================
# Gemini API (Fallback)
# =====================================================================

async def _parse_with_gemini(text_content: str, test_type: str) -> ParsedTest:
    """Parse test using Gemini API."""
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
