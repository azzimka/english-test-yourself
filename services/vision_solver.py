import json
import re
import base64
import logging
from typing import List, Optional, Tuple
from groq import AsyncGroq
from config import GROQ_API_KEY, GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

# Note: Groq vision models are periodically updated or decommissioned by Groq.
GROQ_VISION_MODELS = [
    "llama-3.2-11b-vision-preview",
    "llama-3.2-90b-vision-preview",
]

# Prioritize the user's active configured GEMINI_MODEL
GEMINI_VISION_MODELS = [
    m for m in [GEMINI_MODEL, "gemini-3.6-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    if m
]


async def solve_question_with_vision(
    image_bytes: bytes,
    question_text: str,
    options: List[str]
) -> Optional[Tuple[str, str]]:
    """
    Solves a visual question using Vision AI (Groq Llama-3.2 Vision with Gemini Vision fallback).
    
    Returns:
        (correct_answer, explanation) tuple if solved, or None if vision unavailable/failed.
        Guaranteed NEVER to raise an exception.
    """
    if not image_bytes:
        return None

    # 1. Try Groq Vision API
    if GROQ_API_KEY:
        try:
            client = AsyncGroq(api_key=GROQ_API_KEY)
            b64_img = base64.b64encode(image_bytes).decode("utf-8")
            options_text = "\n".join(options) if options else "(Open question, no multiple-choice options)"

            prompt = (
                "You are an exam solver. Look at the image/diagram and solve the following question.\n\n"
                f"Question:\n{question_text}\n\n"
                f"Options:\n{options_text}\n\n"
                "Return ONLY a JSON object with this exact format:\n"
                "{\n"
                '  "correct_answer": "exact option matching one of the options (e.g. A) 25) or word answer",\n'
                '  "explanation": "brief 1-sentence reasoning"\n'
                "}"
            )

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{b64_img}"
                            }
                        }
                    ]
                }
            ]

            for model_name in GROQ_VISION_MODELS:
                try:
                    logger.info("Attempting Groq Vision solver with %s", model_name)
                    completion = await client.chat.completions.create(
                        model=model_name,
                        messages=messages,
                        response_format={"type": "json_object"},
                        temperature=0.1,
                        max_tokens=512,
                    )
                    content = completion.choices[0].message.content
                    if content:
                        data = json.loads(content)
                        ans = data.get("correct_answer")
                        expl = data.get("explanation")
                        if ans:
                            logger.info("Groq Vision solved question: %s", ans)
                            return str(ans).strip(), str(expl).strip() if expl else "Solved via Vision AI"
                except Exception as e:
                    logger.debug("Groq Vision model %s failed: %s", model_name, e)
        except Exception as e:
            logger.debug("Groq Vision client error: %s", e)

    # 2. Try Gemini Vision fallback
    if GEMINI_API_KEY:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=GEMINI_API_KEY)
            options_text = "\n".join(options) if options else "(Open question)"
            prompt = (
                "You are an exam solver. Look at the diagram and solve this question.\n\n"
                f"Question: {question_text}\n"
                f"Options: {options_text}\n\n"
                "Return ONLY a JSON object with keys 'correct_answer' and 'explanation'."
            )

            for model_name in GEMINI_VISION_MODELS:
                try:
                    logger.info("Attempting Gemini Vision fallback with %s", model_name)
                    response = client.models.generate_content(
                        model=model_name,
                        contents=[
                            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                            prompt
                        ]
                    )
                    raw_text = (response.text or "").strip()
                    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text)
                    clean_json = m.group(1).strip() if m else raw_text
                    data = json.loads(clean_json)
                    ans = data.get("correct_answer")
                    expl = data.get("explanation")
                    if ans:
                        logger.info("Gemini Vision solved question: %s", ans)
                        return str(ans).strip(), str(expl).strip() if expl else "Solved via Vision AI"
                except Exception as e:
                    logger.debug("Gemini Vision model %s failed: %s", model_name, e)
        except Exception as e:
            logger.debug("Gemini Vision client error: %s", e)

    return None
