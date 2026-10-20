import json
import re
from datetime import date
from typing import Optional
from groq import Groq
from src.core.config import settings
from src.core.exceptions import ExtractionError
from src.core.logging import logger
from src.domain.models import LLMExtractionResponse
from src.infrastructure.llm.prompts import EXTRACTION_SYSTEM_PROMPT, FEW_SHOT_EXAMPLES


class GroqLLMClient:
    """Client for structured entity extraction using Groq."""

    def __init__(self, api_key: Optional[str] = None):
        key = api_key or settings.GROQ_API_KEY.get_secret_value()
        if not key:
            logger.warning("GROQ_API_KEY is not configured.")
        self.client = Groq(api_key=key) if key else None

    def _get_candidate_models(self) -> list[str]:
        """Discover available chat models dynamically from Groq account with fallbacks."""
        candidates = []
        if settings.GROQ_LLM_MODEL:
            candidates.append(settings.GROQ_LLM_MODEL)

        try:
            live_models = self.client.models.list()
            chat_models = [
                m.id
                for m in live_models.data
                if "whisper" not in m.id
                and "guard" not in m.id
                and "orpheus" not in m.id
                and "allam" not in m.id
                and "embedding" not in m.id
            ]
            # Prioritize high performance chat models available on Groq
            priority_patterns = [
                "gpt-oss-20b",
                "gpt-oss-120b",
                "qwen3.8",
                "3.3-70b",
                "3.1-8b",
            ]
            for pat in priority_patterns:
                for cm in chat_models:
                    if pat in cm and cm not in candidates:
                        candidates.append(cm)

            for cm in chat_models:
                if cm not in candidates:
                    candidates.append(cm)

        except Exception as e:
            logger.warning(f"Could not list Groq models dynamically: {e}")

        # Static fallbacks
        for fb in [
            "openai/gpt-oss-20b",
            "openai/gpt-oss-120b",
            "qwen/qwen3.8-27b",
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
        ]:
            if fb not in candidates:
                candidates.append(fb)

        return candidates

    @staticmethod
    def _clean_and_parse_json(content: str) -> dict:
        """Parse JSON directly or extract it from markdown/fenced blocks."""
        cleaned = content.strip()
        # 1. Direct parse
        try:
            return json.loads(cleaned)
        except Exception:
            pass

        # 2. Extract from ```json ... ``` markdown
        if "```" in cleaned:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
            if match:
                return json.loads(match.group(1).strip())

        # 3. Extract outermost { ... }
        match = re.search(r"(\{[\s\S]*\})", cleaned)
        if match:
            return json.loads(match.group(1).strip())

        raise json.JSONDecodeError("No valid JSON object found in response", content, 0)

    def extract_expense_entities(
        self,
        text: str,
        reference_date: Optional[date] = None,
        default_currency: str = "USD",
    ) -> LLMExtractionResponse:
        """Send message text to Llama 3 via Groq and parse structured response."""
        if not self.client:
            raise ExtractionError("Groq client is not initialized due to missing GROQ_API_KEY.")

        ref_date = reference_date or date.today()
        user_prompt = (
            f"Fecha de referencia actual: {ref_date.isoformat()}\n"
            f"Moneda por defecto si no se explicita: {default_currency}\n"
            f"Texto o Transcripción recibida: '{text}'"
        )

        messages = [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            *FEW_SHOT_EXAMPLES,
            {"role": "user", "content": user_prompt},
        ]

        candidate_models = self._get_candidate_models()

        last_error = None
        for model_name in candidate_models:
            # Try first with strict json_object, then with prompt-only format
            for use_json_format in [True, False]:
                try:
                    kwargs = {
                        "model": model_name,
                        "messages": messages,
                        "temperature": 0.1,
                        "max_tokens": 1000,
                    }
                    if use_json_format:
                        kwargs["response_format"] = {"type": "json_object"}

                    logger.info(f"Invoking Groq model '{model_name}' (strict_json={use_json_format}).")
                    completion = self.client.chat.completions.create(**kwargs)

                    response_content = completion.choices[0].message.content
                    if not response_content:
                        continue

                    parsed_json = self._clean_and_parse_json(response_content)
                    if "metadata" in parsed_json and "raw_text" not in parsed_json["metadata"]:
                        parsed_json["metadata"]["raw_text"] = text

                    return LLMExtractionResponse.model_validate(parsed_json)

                except Exception as e:
                    last_error = e
                    if use_json_format:
                        logger.warning(
                            f"Groq strict json_format failed on model '{model_name}': {e}. Retrying same model in standard prompt mode..."
                        )
                    else:
                        logger.warning(
                            f"Groq model '{model_name}' failed completely: {e}. Trying next candidate..."
                        )

        logger.error(f"All candidate Groq models failed. Last error: {last_error}")
        raise ExtractionError(f"Groq API extraction failed: {last_error}") from last_error
