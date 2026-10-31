"""Main FastAPI application entrypoint."""

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.api.v1.dependencies import get_expense_repository, get_message_router
from src.api.v1.health import router as health_router
from src.api.v1.webhooks import router as webhooks_router, sheets_router
from src.core.config import settings
from src.core.logging import logger
from src.core.readiness import missing_configuration


async def _prepare_postgres_schema() -> None:
    """Create the schema and the table of the PostgreSQL backend when they are missing (no migrations)."""
    if settings.STORAGE_BACKEND.lower() != "postgres":
        return
    ensure_schema = getattr(get_expense_repository(), "ensure_schema", None)
    if not callable(ensure_schema):
        return
    try:
        await asyncio.to_thread(ensure_schema)
    except Exception as e:
        # A database that is not ready yet must not keep the bot from starting: it retries on the first save
        logger.error(f"Could not prepare the PostgreSQL schema at startup: {e}")


async def _drain_in_flight_messages() -> None:
    """Wait for the messages being processed: Evolution already got its 200, so nobody would resend them."""
    if not get_message_router.cache_info().currsize:
        return
    pending = await get_message_router().wait_until_idle(settings.SHUTDOWN_DRAIN_SECONDS)
    if pending:
        logger.error(f"Shutting down with {pending} message(s) in flight: they may not get an answer.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    logger.info(f"Starting {settings.APP_NAME} in [{settings.ENVIRONMENT}] mode.")
    logger.info(f"Storage backend active: '{settings.STORAGE_BACKEND}'")
    missing = missing_configuration(settings)
    if missing:
        logger.error(
            "Missing configuration: " + ", ".join(missing) + ". The affected flows will fail until it is set "
            "(GET /api/v1/health/ready lists the same names)."
        )
    if not (settings.EVOLUTION_API_KEY.get_secret_value() and settings.EVOLUTION_INSTANCE):
        logger.warning("Evolution API is not configured (EVOLUTION_API_KEY / EVOLUTION_INSTANCE): replies are disabled.")
    if not settings.EVOLUTION_WEBHOOK_SECRET.get_secret_value():
        logger.warning("EVOLUTION_WEBHOOK_SECRET is not set: every webhook call will be rejected.")
    if not settings.SHEETS_WEBHOOK_SECRET.get_secret_value():
        logger.info(
            f"SHEETS_WEBHOOK_SECRET is not set: edits to the users tab apply after the cache expires "
            f"({settings.USER_CACHE_TTL_SECONDS} s) instead of immediately."
        )
    await _prepare_postgres_schema()
    yield
    await _drain_in_flight_messages()
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
            "readiness": "/api/v1/health/ready",
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
