"""
API_Server_Seed.py
──────────────────
Seed-driven devotional generation server.

Responsibilities:
  - Receive pre-built verse data from generate_from_seed.py
  - Call Gemini for reflexion + oracion ONLY
  - Validate Devanagari/non-Latin script output
  - Retry once if script validation fails
  - Return {reflexion, oracion} — nothing else

Python (generate_from_seed.py + DevotionalBuilder) owns:
  - versiculo field construction
  - para_meditar field
  - id generation
  - date, language, version fields
  - tags extraction
  - checkpoint / savepoint
  - final JSON structure

Launch:
  uvicorn API_Server_Seed:app --host 0.0.0.0 --port 50002 --reload
"""

import json
import os
import re
import threading
import time
import traceback
from typing import Optional

import google.generativeai as genai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from google.generativeai.types import HarmBlockThreshold, HarmCategory
from pydantic import BaseModel
from tenacity import RetryError, retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from gemini_rate_limiter import GeminiRateLimiter, GeminiRateLimiterError

# =============================================================================
# STARTUP
# =============================================================================
load_dotenv()

try:
    gemini_api_key = os.environ["GOOGLE_API_KEY"]
except KeyError:
    raise ValueError("GOOGLE_API_KEY not set. Add it to your .env file.")

genai.configure(api_key=gemini_api_key)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# =============================================================================
# GEMINI CONFIG
# =============================================================================
generation_config = genai.types.GenerationConfig(
    temperature=0.7,
    top_p=0.95,
    top_k=64,
    max_output_tokens=2048,   # reduced — only reflexion + oracion needed
)

safety_settings = [
    {"category": HarmCategory.HARM_CATEGORY_HARASSMENT,        "threshold": HarmBlockThreshold.BLOCK_NONE},
    {"category": HarmCategory.HARM_CATEGORY_HATE_SPEECH,       "threshold": HarmBlockThreshold.BLOCK_NONE},
    {"category": HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, "threshold": HarmBlockThreshold.BLOCK_NONE},
    {"category": HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, "threshold": HarmBlockThreshold.BLOCK_NONE},
]

# =============================================================================
# RATE LIMITER
# =============================================================================
_rate_limiter = GeminiRateLimiter(model="gemini-2.5-flash")

# =============================================================================
# SCRIPT VALIDATOR
# Language-aware: checks that Gemini responded in the correct script.
# Latin-script languages (es, en, fr, pt) are always valid — no check needed.
# =============================================================================
SCRIPT_RANGES = {
    "hi": (0x0900, 0x097F),   # Devanagari
    "ja": (0x3040, 0x30FF),   # Hiragana + Katakana
    "zh": (0x4E00, 0x9FFF),   # CJK Unified Ideographs
}

SCRIPT_THRESHOLD = 0.6   # at least 60% of alpha chars must be in target script


def validate_script(text: str, lang: str) -> tuple[bool, float]:
    """
    Returns (is_valid, script_ratio).
    For non-Latin languages: checks that target script ratio > SCRIPT_THRESHOLD.
    For Latin languages (es, en, fr, pt): always returns (True, 1.0).
    """
    if lang not in SCRIPT_RANGES:
        return True, 1.0

    lo, hi = SCRIPT_RANGES[lang]
    alpha_chars = [c for c in text if c.isalpha()]
    if not alpha_chars:
        return False, 0.0

    target_count = sum(1 for c in alpha_chars if lo <= ord(c) <= hi)
    ratio = target_count / len(alpha_chars)
    return ratio >= SCRIPT_THRESHOLD, ratio

# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class SeedGenerateRequest(BaseModel):
    date:            str
    master_lang:     str
    master_version:  str
    versiculo_cita:  str            # e.g. "यूहन्ना 3:16"
    topic:           Optional[str] = None


class CreativeContent(BaseModel):
    reflexion: str
    oracion:   str


class SeedGenerateResponse(BaseModel):
    status:    str
    date:      str
    lang:      str
    reflexion: str
    oracion:   str

# =============================================================================
# GEMINI CALLER
# =============================================================================

@retry(
    wait=wait_exponential(multiplier=1, min=4, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(Exception)
)
async def call_gemini(verse_cita: str, lang: str, topic: Optional[str] = None) -> CreativeContent:
    """
    Minimal Gemini call — returns reflexion + oracion only.
    Verse text is NOT sent. Gemini uses the cita reference as creative context.
    """
    _rate_limiter.acquire()

    model = genai.GenerativeModel(
        "gemini-2.5-flash",
        generation_config=generation_config,
        safety_settings=safety_settings
    )

    # ── Gold prompt — stripped to creative content only ──────────────────────
    prompt_parts = [
        f"You are a devoted biblical devotional writer. "
        f"Write a devotional in {lang.upper()} "
        f"based on the key verse: \"{verse_cita}\".",

        "Return ONLY a valid JSON object with these exact keys:",

        "=== reflexion ===",
        f"- `reflexion`: Deep contextualized reflection on the verse (300 words in {lang}).",

        "=== oracion ===",
        f"- `oracion`: Prayer on the devotional theme (150 words, 100% in {lang}). "
        f"MUST end with 'in the name of Jesus, amen' correctly translated to {lang}.",

        "=== RULES ===",
        f"ALL text MUST be 100% in {lang} — no language mixing.",
        f"Do NOT include transliterations, romanizations, or text in parentheses.",
    ]

    if topic:
        prompt_parts.append(f"Suggested theme: {topic}.")

    print(f"DEBUG: Gemini call — verse: {verse_cita}, lang: {lang}")
    response = await model.generate_content_async(prompt_parts)
    raw = response.text.strip().replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)

    reflexion = data.get("reflexion", "").strip()
    oracion   = data.get("oracion", "").strip()

    if not reflexion or not oracion:
        raise ValueError(f"Gemini returned empty reflexion or oracion for '{verse_cita}'")

    return CreativeContent(reflexion=reflexion, oracion=oracion)


async def call_gemini_fix_script(
    verse_cita: str, lang: str,
    bad_reflexion: str, bad_oracion: str
) -> CreativeContent:
    """
    Single retry call when script validation fails.
    Sends the bad text back to Gemini with explicit correction instruction.
    """
    _rate_limiter.acquire()

    model = genai.GenerativeModel(
        "gemini-2.5-flash",
        generation_config=generation_config,
        safety_settings=safety_settings
    )

    prompt_parts = [
        f"The following devotional text is NOT written in {lang.upper()} script. "
        f"Rewrite it completely in {lang.upper()}. Keep the same devotional meaning.",

        f"Verse: \"{verse_cita}\"",

        f"Wrong reflexion: \"{bad_reflexion[:300]}\"",
        f"Wrong oracion:   \"{bad_oracion[:300]}\"",

        f"Return ONLY: {{\"reflexion\": \"...\", \"oracion\": \"...\"}}",
        f"oracion MUST end with the correct {lang.upper()} translation of "
        f"'in the name of Jesus, amen'.",
        f"ALL text MUST be 100% in {lang} — no language mixing.",
        f"Do NOT include transliterations, romanizations, or text in parentheses.",
    ]

    print(f"WARNING: Script fix retry — verse: {verse_cita}, lang: {lang}")
    response = await model.generate_content_async(prompt_parts)
    raw = response.text.strip().replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)

    return CreativeContent(
        reflexion=data.get("reflexion", bad_reflexion).strip(),
        oracion=data.get("oracion", bad_oracion).strip()
    )

# =============================================================================
# FASTAPI APP
# =============================================================================
app = FastAPI(
    title="Devotional Seed Generator API",
    description="Seed-driven devotional server. Gemini writes reflexion + oracion only.",
    version="1.0.0",
)


@app.post("/generate_creative", response_model=SeedGenerateResponse)
async def generate_creative(request: SeedGenerateRequest):
    """
    Accepts pre-built verse data from generate_from_seed.py.
    Calls Gemini for reflexion + oracion only.
    Validates script — retries once if wrong language detected.
    Returns {reflexion, oracion} — Python builds the rest.
    """
    print(f"\n--- {request.date} | {request.master_lang} | {request.versiculo_cita} ---")

    try:
        # ── Phase 1: Gemini creative call ─────────────────────────────────────
        content = await call_gemini(
            request.versiculo_cita,
            request.master_lang,
            request.topic
        )

        # ── Phase 2: Script validation ────────────────────────────────────────
        reflexion_valid, reflexion_ratio = validate_script(content.reflexion, request.master_lang)
        oracion_valid,   oracion_ratio   = validate_script(content.oracion,   request.master_lang)

        print(f"INFO: Script validation — "
              f"reflexion: {reflexion_ratio:.0%} | oracion: {oracion_ratio:.0%}")

        if not reflexion_valid or not oracion_valid:
            print(f"WARNING: Script validation failed — retrying once")
            try:
                content = await call_gemini_fix_script(
                    request.versiculo_cita,
                    request.master_lang,
                    content.reflexion,
                    content.oracion
                )
                # Validate retry result
                r_valid, r_ratio = validate_script(content.reflexion, request.master_lang)
                o_valid, o_ratio = validate_script(content.oracion,   request.master_lang)
                print(f"INFO: Retry script check — "
                      f"reflexion: {r_ratio:.0%} | oracion: {o_ratio:.0%}")

                if not r_valid or not o_valid:
                    raise ValueError(
                        f"Script validation failed after retry — "
                        f"reflexion: {r_ratio:.0%}, oracion: {o_ratio:.0%}"
                    )
            except Exception as e:
                print(f"ERROR: Script fix retry failed: {e}")
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"SCRIPT_ERROR: {str(e)}"
                )

        print(f"INFO: ✅ {request.date} — {request.versiculo_cita}")

        return SeedGenerateResponse(
            status="success",
            date=request.date,
            lang=request.master_lang,
            reflexion=content.reflexion,
            oracion=content.oracion
        )

    except HTTPException:
        raise
    except GeminiRateLimiterError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"QUOTA_ERROR: {str(e)}"
        )
    except RetryError as e:
        last = e.last_attempt.exception() if e.last_attempt else e
        print(f"ERROR: RetryError for {request.date}: {last}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Gemini retry exhausted: {str(last)}"
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Generation error: {str(e)}"
        )
