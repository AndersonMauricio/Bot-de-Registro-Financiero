"""Healthcheck endpoints for container liveness and readiness."""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from src.core.config import Settings
from src.api.v1.dependencies import get_settings

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("")
async def health_check(settings: Settings = Depends(get_settings)):
    """Return health status and basic system metadata."""
    return {
        "status": "ok",
        "app_name": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "storage_backend": settings.STORAGE_BACKEND,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
