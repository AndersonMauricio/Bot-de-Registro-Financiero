"""Whisper audio transcription client powered by Groq."""

import io
from typing import Optional
from groq import Groq
from src.core.config import settings
from src.core.exceptions import TranscriptionError
from src.core.logging import logger


# Groq infers the audio format from the file extension; WhatsApp sends voice notes as ogg/opus
_AUDIO_EXTENSIONS = {
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
}


def audio_filename_for_mime(mime_type: Optional[str]) -> str:
    """File name with the extension Groq needs, from a mime type such as 'audio/ogg; codecs=opus'."""
    base = (mime_type or "").split(";")[0].strip().lower()
    return f"audio.{_AUDIO_EXTENSIONS.get(base, 'ogg')}"


class WhisperClient:
    """Client for fast speech-to-text transcription using Whisper on Groq."""

    def __init__(self, api_key: Optional[str] = None):
        key = api_key or settings.GROQ_API_KEY.get_secret_value()
        if not key:
            logger.warning("GROQ_API_KEY is missing for WhisperClient.")
        # Same bounded budget as the LLM: a stuck transcription must not hang the conversation
        self.client = (
            Groq(api_key=key, timeout=settings.GROQ_TIMEOUT_SECONDS, max_retries=settings.GROQ_MAX_RETRIES)
            if key
            else None
        )

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
