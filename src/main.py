"""Main FastAPI application entrypoint."""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.api.v1.health import router as health_router
from src.api.v1.webhooks import router as webhooks_router, sheets_router
from src.core.config import settings
from src.core.logging import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    logger.info(f"Starting {settings.APP_NAME} in [{settings.ENVIRONMENT}] mode.")
    logger.info(f"Storage backend active: '{settings.STORAGE_BACKEND}'")
    if not (settings.EVOLUTION_API_KEY.get_secret_value() and settings.EVOLUTION_INSTANCE):
        logger.warning("Evolution API is not configured (EVOLUTION_API_KEY / EVOLUTION_INSTANCE): replies are disabled.")
    if not settings.EVOLUTION_WEBHOOK_SECRET.get_secret_value():
        logger.warning("EVOLUTION_WEBHOOK_SECRET is not set: every webhook call will be rejected.")
    if not settings.SHEETS_WEBHOOK_SECRET.get_secret_value():
        logger.info(
            f"SHEETS_WEBHOOK_SECRET is not set: edits to the users tab apply after the cache expires "
            f"({settings.USER_CACHE_TTL_SECONDS} s) instead of immediately."
        )
    yield
    logger.info("Shutting down application cleanly.")


app = FastAPI(
    title=settings.APP_NAME,
    description="WhatsApp Expense Tracker with LLM and Modern Data Stack Architecture",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS Middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["Root"])
async def root():
    """Root entrypoint with service metadata and available links."""
    return {
        "app": settings.APP_NAME,
        "version": "0.1.0",
        "status": "online",
        "endpoints": {
            "docs": "/docs",
            "health": "/api/v1/health",
            "evolution_webhook": "/api/v1/webhooks/evolution",
            "sheets_webhook": "/api/v1/webhooks/sheets",
        },
    }


# Include v1 API Routers
app.include_router(health_router, prefix="/api/v1")
app.include_router(webhooks_router, prefix="/api/v1")
app.include_router(sheets_router, prefix="/api/v1")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=settings.PORT, reload=settings.DEBUG)
