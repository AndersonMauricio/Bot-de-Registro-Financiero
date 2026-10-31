"""Healthcheck endpoints for container liveness and readiness."""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from src.core.config import Settings
from src.core.readiness import missing_configuration
from src.api.v1.dependencies import get_settings

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("")
async def health_check(settings: Settings = Depends(get_settings)):
    """Liveness: the process answers. This is what the container HEALTHCHECK uses."""
    return {
        "status": "ok",
        "app_name": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "storage_backend": settings.STORAGE_BACKEND,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/ready")
async def readiness_check(settings: Settings = Depends(get_settings)):
    """Readiness: is the service configured well enough to answer users?

    Returns 503 with the names (never the values) of the missing settings. It does not call any external
    service, so a provider throttling or a tunnel down never marks a correctly configured bot as broken.
    """
    missing = missing_configuration(settings)
    if missing:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "missing": missing},
        )
    return {"status": "ready"}

