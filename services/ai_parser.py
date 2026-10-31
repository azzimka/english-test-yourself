import os
import json
import re
import logging
import asyncio
from typing import List, Optional, Tuple, Any
from pydantic import BaseModel, Field, field_validator, model_validator
from groq import AsyncGroq
from config import GROQ_API_KEY, GROQ_MODEL, GEMINI_API_KEY, GEMINI_MODEL, PRIMARY_AI_PROVIDER

logger = logging.getLogger(__name__)


# =====================================================================
# =====================================================================
# Answer Option Matching Helper & Question Type Detection
# =====================================================================

MULTIPLE_CHOICE_REGEX = re.compile(
    r"\b("
    r"choose\s+(?:two|three|four|\d+)"
    r"|select\s+(?:two|three|four|\d+|all)"
    r"|which\s+(?:two|three|four|\d+)"
    r"|tick\s+(?:two|three|four|\d+|all)"
    r"|pick\s+(?:two|three|four|\d+|all)"
    r"|mark\s+(?:two|three|four|\d+|all)"
    r"|выберите\s+(?:два|три|четыре|несколько|все|\d+)"
    r"|отметьте\s+(?:два|три|четыре|несколько|все|\d+)"
    r"|укажите\s+(?:два|три|четыре|несколько|\d+)"
    r"|select\s+all\s+that\s+apply"
    r"|choose\s+all\s+that\s+apply"
    r"|more\s+than\s+one\s+(?:option|answer|variant)"
    r")\b",
    re.IGNORECASE
)


def _match_single_answer(ans: str, options: List[str]) -> Optional[str]:
    """Matches a single candidate answer against the list of options."""
    if not options or not ans:
        return None
    ans_clean = str(ans).strip()
    if not ans_clean:
        return None

    # 1. Exact match
    for opt in options:
        if opt.strip().lower() == ans_clean.lower():
            return opt

    # 2. Single letter match ('A', 'a', 'A)', 'A.')
    m = re.match(r"^([A-Za-zА-Яа-я])(?:[.)\-:]|$)", ans_clean)
    if m:
        letter = m.group(1).upper()
        for opt in options:
            opt_m = re.match(r"^([A-Za-zА-Яа-я])(?:[.)\-:]|$)", opt.strip())
            if opt_m and opt_m.group(1).upper() == letter:
                return opt

    # 3. Normalized text matching
    def _norm(t: str) -> str:
        s = t.strip().lower()
        for prefix in ["a)", "b)", "c)", "d)", "e)", "f)", "g)", "h)", "a.", "b.", "c.", "d.", "e.", "f.", "g.", "h."]:
            if s.startswith(prefix):
                s = s[len(prefix):].strip()
                break
        return s

    norm_ans = _norm(ans_clean)
    if norm_ans:
        for opt in options:
            norm_opt = _norm(opt)
            if norm_ans == norm_opt or (len(norm_ans) > 2 and norm_ans in norm_opt):
                return opt

    return None


def match_answer_to_options_list(ans: Any, options: List[str]) -> List[str]:
    """
    Matches candidate answer(s) to a list of option strings from `options`.
    Supports:
    - Lists of answers: ["A", "C"] or ["A) Apple", "C) Carrot"]
    - Delimited multiple answers: "A, C", "A and C", "15: A, C", "A & D"
    - Space-separated single letters: "A C", "15: B D"
    - Concatenated letters: "AC", "15: BD" (when matching distinct option letters)
    - Single answers: "A", "apple", "A) Apple"
    - Single answers with commas: "A) Yes, I do" (exact option matches take precedence)
    """
    if not options or ans is None:
        if isinstance(ans, list):
            return [str(x).strip() for x in ans if str(x).strip()]
        return [str(ans).strip()] if str(ans or "").strip() else []

    # If already a list
    if isinstance(ans, list):
        matched = []
        for item in ans:
            m = _match_single_answer(str(item), options)
            chosen = m if m else str(item).strip()
            if chosen and chosen not in matched:
                matched.append(chosen)
        matched.sort(key=lambda x: options.index(x) if x in options else 999)
        return matched

    ans_clean = str(ans).strip()
    if not ans_clean:
        return []

    # Strip question number prefix if present (e.g. '15: A, C' or '14-15: B D' or 'Q1: B')
    ans_without_prefix = re.sub(
        r"^(?:Question|Task|Item|Вопрос|Задание|№|Q)?\s*\d+(?:\s*[-–—]\s*\d+)?\s*[:.)\-–—]\s*",
        "",
        ans_clean,
        flags=re.IGNORECASE
    ).strip()

    candidate = ans_without_prefix or ans_clean

    # 1. Try matching the entire string as a single option first (takes priority over comma splitting)
    single_match = _match_single_answer(candidate, options)
    if single_match:
        return [single_match]

    # 2. Tokenize on delimiters: commas, semicolons, slashes, or 'and' / '&' / 'и'
    tokens = [tok.strip() for tok in re.split(r"[,;/]|\s+(?:and|&|и)\s+", candidate) if tok.strip()]
    if len(tokens) <= 1:
        # Check for space-separated single letters: e.g. "A C" or "B D E"
        if re.match(r"^([A-Za-zА-Яа-я](?:\s+[A-Za-zА-Яа-я])+)$", candidate):
            tokens = candidate.split()
        # Check for concatenated letters: e.g. "AC" or "BD"
        elif re.match(r"^([A-Za-zА-Яа-я]{2,5})$", candidate):
            candidate_chars = list(candidate)
            matches = [_match_single_answer(c, options) for c in candidate_chars]
            if all(m is not None for m in matches) and len(set(matches)) == len(matches):
                tokens = candidate_chars

    if len(tokens) > 1:
        matched_multi = []
        for tok in tokens:
            m = _match_single_answer(tok, options)
            if m and m not in matched_multi:
                matched_multi.append(m)
        if len(matched_multi) >= 2 or (len(matched_multi) == len(tokens) and len(matched_multi) > 0):
            matched_multi.sort(key=lambda x: options.index(x) if x in options else 999)
            return matched_multi

    # 3. Fallback on original string
    s_orig = _match_single_answer(ans_clean, options)
    if s_orig:
        return [s_orig]

    return [ans_clean] if ans_clean else []


def match_answer_to_options(ans: Any, options: List[str]) -> str:
    """
    Matches candidate answer(s) to a comma-separated string of matched options.
    """
    matched = match_answer_to_options_list(ans, options)
    return ", ".join(matched)


# =====================================================================
# Pydantic Models
# =====================================================================

class ParsedQuestion(BaseModel):
    question_text: str = Field(default="Question", description="Текст вопроса или задание с пропуском")
    context_text: Optional[str] = Field(default=None, description="Текст для чтения, если несколько вопросов относятся к одному рассказу/статье")
    options: List[str] = Field(default_factory=list, description="Список вариантов ответа (например, ['A) apple', 'B) banana']). Если вопрос с открытым вводом слова, список пуст.")
    correct_answer: str = Field(default="", description="Правильный вариант ответа или слово. Для множественного выбора содержит все правильные ответы через запятую.")
    correct_answers: Optional[List[str]] = Field(default=None, description="Список правильных ответов (для вопросов с множественным выбором)")
    question_type: str = Field(default="single_choice", description="Тип вопроса: 'single_choice', 'multiple_choice' или 'open_text'")
    explanation: Optional[str] = Field(default=None, description="Краткое пояснение грамматического или лексического правила")
    image_url: Optional[str] = Field(default=None, description="Относительный URL к изображению задания")

    @model_validator(mode="before")
    @classmethod
    def pre_validate_question(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # 1. If correct_answers is provided but correct_answer is missing or empty
            ca = data.get("correct_answer")
            cas = data.get("correct_answers")
            if (ca is None or str(ca).strip() == "") and cas:
                if isinstance(cas, list):
                    data["correct_answer"] = ", ".join(str(x).strip() for x in cas if str(x).strip())
                else:
                    data["correct_answer"] = str(cas).strip()

            # 2. Normalize question_type if passed
            qt = data.get("question_type")
            if qt:
                qt_str = str(qt).strip().lower()
                if qt_str in ("multiple_choice", "multiple", "multi_choice", "multiple-choice", "multichoice", "multiple choice"):
                    data["question_type"] = "multiple_choice"
                elif qt_str in ("open_text", "open", "text", "cloze", "word_formation", "gap_fill", "open text"):
                    data["question_type"] = "open_text"
                elif qt_str in ("single_choice", "single", "single-choice", "choice", "single choice"):
                    data["question_type"] = "single_choice"
        return data

    @field_validator("question_text", mode="before")
    @classmethod
    def sanitize_question_text(cls, v: Any) -> str:
        if v is None:
            return "Question"
        s = str(v).strip()
        if not s:
            return "Question"
        # If question text has runs of 2+ spaces (lost gap in PDF extraction) and lacks _____
        if "____" not in s:
            s = re.sub(r"(?<=[a-zA-Z0-9,\'’])\s{2,}(?=[a-zA-Z0-9.\'’])", " _____ ", s)
            s = re.sub(r"(?<=:\s)\s{2,}(?=[a-zA-Z0-9])", " _____ ", s)
        return s

    @field_validator("options", mode="before")
    @classmethod
    def sanitize_options(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, dict):
            result = []
            for key, val in v.items():
                if val is not None:
                    s_val = str(val).strip()
                    s_key = str(key).strip()
                    if s_val:
                        if s_key and not re.match(rf"^{re.escape(s_key)}\s*[.):\-]\s*", s_val, re.IGNORECASE):
                            result.append(f"{s_key}) {s_val}")
                        else:
                            result.append(s_val)
            return result
        if isinstance(v, list):
            cleaned = []
            for item in v:
                if item is None:
                    continue
                if isinstance(item, dict):
                    label = item.get("label") or item.get("option") or item.get("key") or ""
                    val = item.get("text") or item.get("value") or item.get("val") or ""
                    if val:
                        s_val = str(val).strip()
                        s_label = str(label).strip()
                        if s_label and not re.match(rf"^{re.escape(s_label)}\s*[.):\-]\s*", s_val, re.IGNORECASE):
                            cleaned.append(f"{s_label}) {s_val}")
                        else:
                            cleaned.append(s_val)
                    elif label:
                        cleaned.append(str(label).strip())
                else:
                    s = str(item).strip()
                    if s:
                        cleaned.append(s)
            return cleaned
        if isinstance(v, str) and v.strip():
            lines = [line.strip() for line in v.split("\n") if line.strip()]
            return lines if len(lines) > 1 else [v.strip()]
        return []

    @field_validator("correct_answer", mode="before")
    @classmethod
    def sanitize_correct_answer(cls, v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, (int, float, bool)):
            return str(v)
        if isinstance(v, list):
            items = []
            for item in v:
                if item is not None:
                    s = cls.sanitize_correct_answer(item)
                    if s:
                        items.append(s)
            return ", ".join(items)
        if isinstance(v, dict):
            for k in ("correct_answers", "correct_answer", "answer", "value", "val", "text", "option", "key"):
                if k in v and v[k] is not None:
                    return cls.sanitize_correct_answer(v[k])
            for val in v.values():
                if val is not None:
                    return cls.sanitize_correct_answer(val)
            return ""
        return str(v).strip()

    @field_validator("correct_answers", mode="before")
    @classmethod
    def sanitize_correct_answers(cls, v: Any) -> Optional[List[str]]:
        if v is None:
            return None
        if isinstance(v, list):
            cleaned = []
            for item in v:
                if item is not None:
                    s = str(item).strip()
                    if s:
                        cleaned.append(s)
            return cleaned
        if isinstance(v, str) and v.strip():
            parts = [p.strip() for p in v.split(",") if p.strip()]
            return parts if parts else [v.strip()]
        return None

    @field_validator("explanation", "context_text", "image_url", mode="before")
    @classmethod
    def sanitize_nullable_strings(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    @model_validator(mode="after")
    def resolve_missing_answer(self) -> "ParsedQuestion":
        ans = (self.correct_answer or "").strip()

        # Step 1: Open Cloze without options
        if not self.options:
            self.question_type = "open_text"
            self.correct_answers = [self.correct_answer] if self.correct_answer else []
            return self

        # Step 2: Determine if multiple choice
        is_multi = False
        if self.question_type == "multiple_choice":
            is_multi = True
        elif self.correct_answers and len(self.correct_answers) > 1:
            is_multi = True
        elif MULTIPLE_CHOICE_REGEX.search(self.question_text):
            is_multi = True

        if not ans or ans.lower() in ("none", "null", "n/a", "unknown", "undefined"):
            if self.options:
                self.correct_answer = self.options[0]
                self.correct_answers = [self.options[0]]
            else:
                self.correct_answer = ""
                self.correct_answers = []
        else:
            source = self.correct_answers if (self.correct_answers and len(self.correct_answers) > 1) else ans
            matched_list = match_answer_to_options_list(source, self.options)
            if matched_list:
                self.correct_answer = ", ".join(matched_list)
                self.correct_answers = matched_list
                if len(matched_list) > 1:
                    is_multi = True
            else:
                self.correct_answer = ans
                self.correct_answers = [ans] if ans else []

        if is_multi:
            self.question_type = "multiple_choice"
            if not self.correct_answers:
                self.correct_answers = [self.correct_answer] if self.correct_answer else []
        else:
            if self.question_type not in ("single_choice", "multiple_choice", "open_text"):
                self.question_type = "single_choice"
            if self.correct_answers is None and self.correct_answer:
                self.correct_answers = [self.correct_answer]

        return self


class ParsedTest(BaseModel):
    title: str = Field(default="English Test", description="Понятное название теста (например, 'Past Tenses Quiz' или 'Олимпиада 9 класс — Reading & Grammar')")
    description: Optional[str] = Field(default="", description="Краткое описание темы теста")
    questions: List[ParsedQuestion] = Field(default_factory=list, description="Список всех распознанных вопросов")
    has_answer_keys: bool = Field(default=False, description="Присутствовали ли ключи к ответам в исходных материалах теста")

    @field_validator("title", mode="before")
    @classmethod
    def sanitize_title(cls, v: Any) -> str:
        if v is None or not str(v).strip():
            return "English Test"
        return str(v).strip()

    @field_validator("description", mode="before")
    @classmethod
    def sanitize_description(cls, v: Any) -> str:
        return str(v).strip() if v is not None else ""

    @field_validator("questions", mode="before")
    @classmethod
    def sanitize_questions(cls, v: Any) -> list:
        if v is None:
            return []
        return v if isinstance(v, list) else []

    @field_validator("has_answer_keys", mode="before")
    @classmethod
    def sanitize_has_answer_keys(cls, v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "да")
        return bool(v)


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
5. Identify question_type:
   - "multiple_choice": when a question has options AND requires selecting TWO or more correct answers (e.g. "Choose TWO correct answers", "Select all that apply", or answer key has multiple answers like "15: A, C").
   - "single_choice": standard question with only ONE correct option.
   - "open_text": open cloze / word formation task without options.
6. Identify the correct answer: look for asterisks (*), pluses (+), bold text, or answer keys at the bottom/end of the materials. If no keys are provided, determine the correct answer yourself.
   - For "multiple_choice", provide all correct answers separated by commas (e.g. "A) apple, C) banana" or "A, C") or list in `correct_answers`.
7. Provide a concise explanation for the correct answer where appropriate.
8. Generate a concise, suitable title for the test.
9. RESTORING BLANKS AND GAPS IN QUESTIONS (CRITICAL):
   For all fill-in-the-blank or multiple-choice questions, the sentence MUST have the missing word position explicitly marked with `_____` (5 underscores, e.g. "Mother: I _____ buy one if we had the money").
   In PDF materials, blanks are often represented by multiple spaces (e.g. "he’s   London" or "I   buy"), dots, or omitted entirely. You MUST detect where the chosen option fits and insert `_____` at the grammatically correct place. NEVER output a fill-in-the-blank question without `_____`!
10. QUESTION 0 AND EXAMPLES:
   If a question is numbered '0' or marked as an example with multiple-choice options (A, B, C, D), PARSE IT as question 0 so students can solve it. If an answer letter was printed in the gap (e.g. "I A going"), convert it back to a blank `_____` ("I _____ going").

CRITICAL: Output MUST be a valid JSON object matching this exact structure:
{
  "title": "string",
  "description": "string",
  "questions": [
    {
      "question_text": "string",
      "context_text": "string or null",
      "question_type": "single_choice | multiple_choice | open_text",
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
1. Parse EVERY SINGLE question present in this batch without omitting any. If a question is numbered '0' or marked as an example with options (A, B, C, D), INCLUDE IT as question 0 so students can solve it. Ensure any demo answer filled in (e.g. 'I A going') is converted back to a blank '_____' ('I _____ going').
2. If multiple-choice options (A, B, C, D) exist, put them into `options` (formatted as 'A) ...', 'B) ...', etc.).
3. If it is an open cloze / word formation task (fill in the blank without multiple choice), leave `options` as an empty list [].
4. Set `question_type`:
   - "multiple_choice": if the question requires selecting multiple answers (e.g. "Choose TWO", "Select all", or answer key has multiple options like "A, C").
   - "single_choice": if standard question with 1 correct option.
   - "open_text": if open cloze / word formation with empty options.
5. If an Answer Key is provided below, use it to accurately fill `correct_answer`. For multiple choice, include all correct options (e.g. "A, C" or "A) ... , C) ...").
6. If no answer key is provided, determine the correct answer yourself.
7. Keep `explanation` very brief (1 short sentence) or null to save tokens.
8. If questions refer to a reading passage in this batch, include that text in `context_text`.
9. GAPS / BLANKS IN QUESTIONS (CRITICAL):
   Every multiple-choice or cloze question MUST have its blank explicitly written as `_____` (5 underscores, e.g. 'Mother: I _____ buy one if...'). If the source text omitted the blank or has spaces (e.g. 'I   buy' or 'he’s   London'), you MUST restore and insert `_____` at the exact grammatically correct position. NEVER output a question without `_____`!

Output MUST be a valid JSON object:
{
  "questions": [
    {
      "question_text": "string",
      "context_text": "string or null",
      "question_type": "single_choice | multiple_choice | open_text",
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
MAX_SINGLE_CHARS = 3500  # Max character length for single request (safety threshold against token cutoffs)

# Fallback models for Groq API
GROQ_FALLBACK_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]

# Regex patterns
QUESTION_START_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:Question|Task|Exercise|Задание|Вопрос|Item|№)?\s*(\d+)\s*(?:[.):\-]\s*|\s+(?=[A-ZА-ЯЁ]))",
    re.MULTILINE
)

# Header pattern matching candidate Answer Key section headings
ANSWER_KEY_HEADER_PATTERN = re.compile(
    r"(?:^|\n)[ \t*=\-#_]*(?:"
    r"ANSWER\s*KEY[S]?"
    r"|ANSWER\s*SHEET[S]?"
    r"|CORRECT\s*ANSWERS?"
    r"|ПРАВИЛЬНЫЕ\s*ОТВЕТЫ"
    r"|(?:ANSWERS?|KEYS?|ОТВЕТЫ|КЛЮЧИ)(?:\s*(?:&|and|и)\s*(?:KEYS?|ANSWERS?|ОТВЕТЫ|КЛЮЧИ))?"
    r"(?:\s+(?:to|к)\s+[^\n:]+)?"
    r")\b[ \t*=\-#_]*(?:[:\-–—]|\n|$)",
    re.IGNORECASE
)

# Pattern verifying that the candidate block actually contains answer key entries
# (e.g. "1. A", "1) went", "1 - True", "1: B", "1 A 2 B", "Q1: C")
ANSWER_KEY_ENTRY_PATTERN = re.compile(
    r"(?:^|\s|[.,;:(\[])(?:Q|q|Question|Task|Задание|Вопрос|Item|№)?\s*(\d{1,3})\s*[:.)\-–—]\s*(\S+)"
    r"|\b(\d{1,3})\s*[:.)\-–—]?\s*([A-Ha-h]|True|False|Not Given|T|F)\b",
    re.IGNORECASE
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
        If no answer key found or candidate section has no valid keys,
        answer_keys_text is empty string and body_text is the original text.
    """
    if not text or not text.strip():
        return text, ""

    matches = list(ANSWER_KEY_HEADER_PATTERN.finditer(text))
    # Iterate from the end of the document backwards
    for m in reversed(matches):
        candidate_keys = text[m.start():].strip()
        # Verify candidate block has actual key entries
        if ANSWER_KEY_ENTRY_PATTERN.search(candidate_keys):
            body_text = text[:m.start()].strip()
            return body_text, candidate_keys

    return text, ""


def extract_max_key_number(answer_keys_text: str) -> int:
    """
    Extract the highest question number found in the Answer Key block.
    Supports formats like '1 A', '1. A', '1) B', 'Q1: C', etc.
    """
    if not answer_keys_text:
        return 0
    matches = re.findall(
        r"(?:^|\s|[.,;:(\[])(?:Q|q|Question|Task|Задание|Item|№)?\s*(\d+)\s*[:.)\-]?\s*([A-Ha-h]|True|False|Not Given|T|F)\b",
        answer_keys_text,
        re.IGNORECASE
    )
    if matches:
        return max(int(m[0]) for m in matches)
    nums = re.findall(r"\b(\d{1,3})\b", answer_keys_text)
    valid_nums = [int(n) for n in nums if 1 <= int(n) <= 500]
    return max(valid_nums) if valid_nums else 0


def estimate_test_scale(body_text: str, answer_keys_text: str = "") -> Tuple[int, int, bool]:
    """
    Estimates the scale of the test based on 3 independent signals:
    1. Maximum question number in Answer Key
    2. Detected question start markers via regex
    3. Character volume of the document (Safety Net)

    Returns:
        Tuple of (estimated_question_count, max_key_number, is_large_test)
    """
    detected_count = len(_find_question_positions(body_text))
    max_key = extract_max_key_number(answer_keys_text)
    char_len = len(body_text.strip())

    estimated_count = max(detected_count, max_key)
    if estimated_count == 0 and char_len > MAX_SINGLE_CHARS:
        estimated_count = max(26, char_len // 200)

    is_large = (
        detected_count > MAX_SINGLE_BATCH
        or max_key > MAX_SINGLE_BATCH
        or char_len > MAX_SINGLE_CHARS
    )

    return estimated_count, max_key, is_large


def _find_question_positions(text: str) -> List[Tuple[int, int]]:
    """
    Find all question start positions in the text.

    Returns:
        List of (question_number, char_position) tuples sorted by position.
    """
    positions = []
    for match in QUESTION_START_PATTERN.finditer(text):
        q_num = int(match.group(1))
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


def split_test_by_windows(
    text: str,
    max_chunk_chars: int = 3500,
    overlap_chars: int = 350
) -> List[str]:
    """
    Fallback chunking for documents without clear question numbers.
    Splits text by paragraphs into chunks of ~max_chunk_chars
    with overlap_chars overlap to prevent splitting questions across boundaries.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chunk_chars:
        return [text]

    paragraphs = re.split(r"(\n\s*\n+)", text)
    chunks = []
    current_chunk = []
    current_len = 0

    for part in paragraphs:
        part_len = len(part)
        if current_len + part_len > max_chunk_chars and current_chunk:
            chunk_str = "".join(current_chunk).strip()
            if chunk_str:
                chunks.append(chunk_str)
            # Retain tail for overlap
            overlap_accum = []
            overlap_len = 0
            for item in reversed(current_chunk):
                overlap_accum.insert(0, item)
                overlap_len += len(item)
                if overlap_len >= overlap_chars:
                    break
            current_chunk = overlap_accum + [part]
            current_len = sum(len(x) for x in current_chunk)
        else:
            current_chunk.append(part)
            current_len += part_len

    if current_chunk:
        chunk_str = "".join(current_chunk).strip()
        if chunk_str and (not chunks or chunk_str != chunks[-1]):
            chunks.append(chunk_str)

    return chunks


def split_test_into_chunks(body_text: str, chunk_size: int = CHUNK_SIZE) -> List[str]:
    """
    Split the test body text into chunks of approximately `chunk_size` questions.

    Rules:
    - Never split a question in the middle.
    - Keep reading passages together with their associated questions.
    - Target chunk_size questions per chunk.
    - If no or few question markers are detected in a large text, fall back to paragraph windowing.

    Returns:
        List of text chunks.
    """
    question_positions = _find_question_positions(body_text)
    char_len = len(body_text.strip())

    if not question_positions:
        # Fallback to paragraph windowing if text is large but no question positions found
        if char_len > MAX_SINGLE_CHARS:
            return split_test_by_windows(body_text, max_chunk_chars=3500, overlap_chars=350)
        return [body_text.strip()] if body_text.strip() else []

    total_questions = len(question_positions)

    # If document is huge (>7000 chars) but we only found a tiny handful of markers,
    # regex likely missed most questions -> use windowing fallback
    if char_len > MAX_SINGLE_CHARS * 2 and total_questions < 10:
        return split_test_by_windows(body_text, max_chunk_chars=3500, overlap_chars=350)

    if total_questions <= chunk_size:
        if char_len > MAX_SINGLE_CHARS * 2:
            return split_test_by_windows(body_text, max_chunk_chars=3500, overlap_chars=350)
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


async def _attach_images_and_vision_solutions(
    questions: List[ParsedQuestion],
    images: Optional[List[Any]]
) -> None:
    """
    Correlates extracted images/diagrams with parsed test questions.
    Attaches image_url to matching questions and invokes Vision AI to solve
    diagram-based questions if correct_answer was missing or undetermined.
    """
    if not images or not questions:
        return

    from services.vision_solver import solve_question_with_vision

    visual_kw = re.compile(
        r"\b(picture|image|figure|diagram|illustration|drawing|triangle|shape|graph|рисунок|схема|чертеж|фигура|на рисунке)\b",
        re.IGNORECASE
    )

    unassigned_images = list(images)

    # 1. Exact question number matching
    for idx, q in enumerate(questions, start=1):
        # Determine question number from order or question_text
        match_q_num = idx
        m = re.match(
            r"^(?:Question|Task|Exercise|Problem|Задание|Вопрос|Item|№)?\s*(\d+)",
            q.question_text.strip(),
            re.IGNORECASE
        )
        if m:
            match_q_num = int(m.group(1))

        matched_img = None
        for img in unassigned_images:
            if getattr(img, "question_num", None) == match_q_num:
                matched_img = img
                break

        if matched_img:
            q.image_url = matched_img.image_url
            unassigned_images.remove(matched_img)
            logger.info("Attached image %s to Question #%d", matched_img.image_url, match_q_num)

            # Vision AI solver: If question mentions visual cues or has image, solve with Vision AI
            if getattr(matched_img, "image_bytes", None):
                try:
                    vision_result = await solve_question_with_vision(
                        image_bytes=matched_img.image_bytes,
                        question_text=q.question_text,
                        options=q.options
                    )
                    if vision_result:
                        ans, expl = vision_result
                        if ans:
                            q.correct_answer = match_answer_to_options(ans, q.options) if q.options else ans
                            if expl:
                                q.explanation = expl
                            logger.info("Vision AI updated correct_answer for Question #%d: %s", match_q_num, q.correct_answer)
                except Exception as e:
                    logger.debug("Vision solver attempt failed for Question #%d: %s", match_q_num, e)

    # 2. Text reference matching for remaining unassigned images
    if unassigned_images:
        for idx, q in enumerate(questions, start=1):
            if q.image_url:
                continue
            if visual_kw.search(q.question_text) and unassigned_images:
                img = unassigned_images.pop(0)
                q.image_url = img.image_url
                logger.info("Attached image %s to Question #%d via keyword match", img.image_url, idx)
                if getattr(img, "image_bytes", None):
                    try:
                        vision_result = await solve_question_with_vision(
                            image_bytes=img.image_bytes,
                            question_text=q.question_text,
                            options=q.options
                        )
                        if vision_result:
                            ans, expl = vision_result
                            if ans:
                                q.correct_answer = match_answer_to_options(ans, q.options) if q.options else ans
                                if expl:
                                    q.explanation = expl
                    except Exception as e:
                        logger.debug("Vision solver attempt failed: %s", e)


# =====================================================================
# Main Entry Point
# =====================================================================

async def parse_test_content(
    text_content: str,
    test_type: str = "regular",
    images: Optional[List[Any]] = None
) -> ParsedTest:
    """
    Parses test content using Groq API with intelligent chunking for large tests.
    Falls back to Gemini API if configured and Groq fails.

    For tests with <= 25 questions: single request (fast path).
    For tests with > 25 questions: parallel chunked processing via asyncio.gather.
    Attaches any extracted images and resolves visual questions via Vision AI.
    """
    if not text_content or not text_content.strip():
        raise ValueError("Содержимое теста пусто или не удалось извлечь текст.")

    # --- Step 1: Extract answer keys from the end of the document ---
    body_text, answer_keys_text = extract_answer_keys(text_content)
    has_keys = bool(answer_keys_text and answer_keys_text.strip())
    if answer_keys_text:
        logger.info("Answer key section found and extracted (%d chars)", len(answer_keys_text))

    # --- Step 2: Multi-signal test scale estimation ---
    estimated_count, max_key, is_large_test = estimate_test_scale(body_text, answer_keys_text)
    logger.info(
        "Test scale evaluation: estimated_count=%d, max_key=%d, char_len=%d, is_large=%s",
        estimated_count, max_key, len(body_text), is_large_test
    )

    # Build the model fallback chain
    models = _build_model_chain()

    # --- Primary Gemini Provider branch (if configured) ---
    if PRIMARY_AI_PROVIDER == "gemini" and GEMINI_API_KEY and not os.getenv("PYTEST_CURRENT_TEST"):
        try:
            logger.info("PRIMARY_AI_PROVIDER is set to 'gemini'. Processing directly with Gemini API...")
            parsed = await _parse_with_gemini(text_content, test_type)
            parsed.has_answer_keys = has_keys
            if images and parsed and parsed.questions:
                await _attach_images_and_vision_solutions(parsed.questions, images)
            return parsed
        except Exception as e:
            logger.warning("Gemini primary parsing failed: %s. Falling back to Groq pipeline...", e)

    # --- Chunked path: large test -> parallel processing ---
    if is_large_test:
        logger.info(
            "Large test detected (%d estimated questions / %d chars). Using chunked parallel processing.",
            estimated_count, len(body_text)
        )
        parsed = await _parse_chunked(body_text, answer_keys_text, test_type, models, expected_count=estimated_count)
    else:
        # --- Fast path: small test (<= 25 questions AND <= 3500 chars) ---
        logger.info("Small test detected (%d questions). Using fast path single request.", estimated_count)
        parsed = await _parse_single_request(text_content, test_type, models)

        # --- Auto-Healing Audit for Fast Path ---
        target_count = max(estimated_count, max_key)
        if target_count > 0 and len(parsed.questions) < int(target_count * 0.85):
            logger.warning(
                "Auto-Healing triggered! Fast path parsed %d questions, but expected ~%d. "
                "Rerunning with parallel chunked parser...",
                len(parsed.questions), target_count
            )
            parsed = await _parse_chunked(body_text, answer_keys_text, test_type, models, expected_count=target_count)

    # Ensure has_answer_keys is explicitly set on the parsed test
    parsed.has_answer_keys = has_keys

    # --- Attach extracted images and run Vision AI if applicable ---
    if images and parsed and parsed.questions:
        await _attach_images_and_vision_solutions(parsed.questions, images)

    return parsed


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
            _, answer_keys_text = extract_answer_keys(text_content)
            data["has_answer_keys"] = bool(answer_keys_text and answer_keys_text.strip())
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
    expected_count: int = 0,
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
    has_keys = bool(answer_keys_text and answer_keys_text.strip())
    parsed = ParsedTest(
        title=title,
        description=description or "",
        questions=parsed_questions,
        has_answer_keys=has_keys,
    )

    logger.info(
        "Successfully parsed chunked test: '%s' with %d questions from %d chunks",
        title, len(parsed_questions), len(chunks)
    )

    # --- Verify numbering completeness ---
    _verify_question_completeness(parsed_questions, body_text, expected_count=expected_count)

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


def _verify_question_completeness(
    questions: List[ParsedQuestion],
    body_text: str,
    expected_count: int = 0
):
    """
    Log verification of question completeness against detected positions or expected count.
    """
    detected = count_questions_in_text(body_text)
    expected = max(detected, expected_count)
    actual = len(questions)

    if expected > 0 and actual < int(expected * 0.85):
        logger.warning(
            "Question count mismatch! Expected ~%d questions but parsed %d. "
            "Some questions may have been lost.",
            expected, actual
        )
    else:
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

    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    contents = [
        f"Тип теста: {test_type}. {PROMPT_SYSTEM}",
        f"Материалы теста:\n\n{text_content}"
    ]

    models_to_try = [GEMINI_MODEL, "gemini-3.5-flash-lite", "gemini-3.7-flash"]
    seen_models = set()
    unique_models = []
    for m in models_to_try:
        if m and m not in seen_models:
            seen_models.add(m)
            unique_models.append(m)

    last_err = None
    for m in unique_models:
        try:
            logger.info("Attempting test parsing with Gemini model: %s", m)
            response = await gemini_client.aio.models.generate_content(
                model=m,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ParsedTest,
                    temperature=0.2,
                )
            )
            _, answer_keys_text = extract_answer_keys(text_content)
            has_keys = bool(answer_keys_text and answer_keys_text.strip())
            if response.parsed:
                logger.info("Successfully parsed test with Gemini %s (%s questions)", m, len(response.parsed.questions))
                response.parsed.has_answer_keys = has_keys
                return response.parsed
            data = json.loads(response.text)
            data["has_answer_keys"] = has_keys
            parsed = ParsedTest(**data)
            logger.info("Successfully parsed test with Gemini %s (%s questions)", m, len(parsed.questions))
            return parsed
        except Exception as e:
            logger.warning("Gemini model %s failed: %s", m, e)
            last_err = e

    raise last_err or ValueError("All Gemini models failed")
