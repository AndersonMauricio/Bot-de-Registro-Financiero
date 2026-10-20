"""Whisper audio transcription client powered by Groq."""

import io
from typing import Optional
from groq import Groq
from src.core.config import settings
from src.core.exceptions import TranscriptionError
from src.core.logging import logger


class WhisperClient:
    """Client for fast speech-to-text transcription using Whisper on Groq."""

    def __init__(self, api_key: Optional[str] = None):
        key = api_key or settings.GROQ_API_KEY.get_secret_value()
        if not key:
            logger.warning("GROQ_API_KEY is missing for WhisperClient.")
        self.client = Groq(api_key=key) if key else None

    def transcribe_audio_bytes(
        self,
        audio_bytes: bytes,
        filename: str = "audio.ogg",
        prompt: Optional[str] = None,
    ) -> str:
        """Transcribe raw audio bytes using Groq Whisper endpoint."""
        if not self.client:
            raise TranscriptionError("WhisperClient is not initialized (missing API Key).")

        try:
            logger.info(f"Sending {len(audio_bytes)} bytes of audio to Groq Whisper ({settings.GROQ_WHISPER_MODEL}).")
            audio_file = (filename, io.BytesIO(audio_bytes))

            transcription = self.client.audio.transcriptions.create(
                file=audio_file,
                model=settings.GROQ_WHISPER_MODEL,
                prompt=prompt or "Registro de gastos, compras y transacciones financieras.",
                response_format="text",
                language="es",
                temperature=0.0,
            )

            result_text = str(transcription).strip()
            logger.info(f"Transcription completed: '{result_text[:60]}...'")
            return result_text

        except Exception as e:
            logger.error(f"Whisper transcription error: {e}")
            raise TranscriptionError(f"Failed to transcribe audio: {e}") from e
