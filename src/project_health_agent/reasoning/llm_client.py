"""
Unified LLM client supporting Groq and Google Gemini, chosen via
LLM_PROVIDER in .env. Every call asks for structured JSON matching a given
schema description.

Design choice: if no provider/key is configured (or a call fails), this
falls back to a deterministic, template-based generator instead of raising.
This keeps the whole pipeline runnable for grading/demo purposes without a
key, while producing real model-backed reasoning as soon as GROQ_API_KEY or
GEMINI_API_KEY is set. The fallback is clearly labeled in every output via
"generated_by": "fallback_template" so it is never mistaken for a model
response.
"""
from __future__ import annotations

import json
import re

from project_health_agent.core.config import settings
from project_health_agent.core.logging_config import get_logger

logger = get_logger("reasoning.llm_client")


def _extract_json(text: str) -> dict:
    """LLMs sometimes wrap JSON in prose or code fences despite instructions;
    this pulls out the first {...} block defensively."""
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in LLM response: {text[:200]}")
    return json.loads(match.group())


def _call_groq(system_prompt: str, user_prompt: str) -> dict:
    from groq import Groq
    client = Groq(api_key=settings.GROQ_API_KEY)
    resp = client.chat.completions.create(
        model=settings.GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content or "{}")


def _call_gemini(system_prompt: str, user_prompt: str) -> dict:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    resp = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    return _extract_json(resp.text or "")


def generate_json(system_prompt: str, user_prompt: str, fallback_fn) -> dict:
    """
    Attempts a real LLM call per LLM_PROVIDER; on any failure (no key, network
    error, malformed response) falls back to fallback_fn(), a zero-argument
    callable that deterministically builds an equivalent-shape response from
    the same inputs already embedded in the closure. Callers pass the
    fallback logic in, so this module stays provider-agnostic.
    """
    try:
        if settings.LLM_PROVIDER == "groq" and settings.GROQ_API_KEY:
            result = _call_groq(system_prompt, user_prompt)
            result["generated_by"] = f"groq:{settings.GROQ_MODEL}"
            return result
        if settings.LLM_PROVIDER == "gemini" and settings.GEMINI_API_KEY:
            result = _call_gemini(system_prompt, user_prompt)
            result["generated_by"] = f"gemini:{settings.GEMINI_MODEL}"
            return result
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any LLM failure should degrade gracefully
        logger.warning("LLM call failed (%s); falling back to deterministic template.", exc)
        result = fallback_fn()
        result["generated_by"] = "fallback_template"
        result["fallback_reason"] = f"{type(exc).__name__}: {exc}"
        return result

    # No provider configured at all
    logger.info("No LLM provider configured; using deterministic fallback template.")
    result = fallback_fn()
    result["generated_by"] = "fallback_template"
    result["fallback_reason"] = "No LLM_PROVIDER/API key configured (see .env.example)."
    return result
