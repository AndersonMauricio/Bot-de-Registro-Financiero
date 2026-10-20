"""Custom domain and infrastructure exceptions."""


class BotWppException(Exception):
    """Base exception for WhatsApp Bot Application."""
    pass


class TranscriptionError(BotWppException):
    """Raised when audio transcription with Whisper fails."""
    pass


class ExtractionError(BotWppException):
    """Raised when entity extraction via LLM fails."""
    pass


class DataQualityValidationError(BotWppException):
    """Raised when an expense record fails data quality gate."""

    def __init__(self, message: str, reasons: list[str]):
        super().__init__(message)
        self.reasons = reasons


class StorageError(BotWppException):
    """Raised when repository fails to persist an expense."""
    pass


class WhatsAppClientError(BotWppException):
    """Raised when interaction with WhatsApp Cloud API fails."""
    pass
