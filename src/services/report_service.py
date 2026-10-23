"""Data serving: delivers each user a private spreadsheet with their own expenses."""

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
from src.core.config import settings
from src.core.logging import logger
from src.core.phone import mask_phone
from src.infrastructure.cache.ttl_cache import TTLCache
from src.infrastructure.storage.base import ExpenseRepositoryInterface, ReportPublisherInterface, UserRepositoryInterface
from src.services.data_quality import DataQualityGate

NO_EMAIL_MESSAGE = (
    "📧 Para enviarte el reporte necesito tu email, y todavía no lo tengo registrado. "
    "Escribile al administrador para que lo cargue."
)
NO_EXPENSES_MESSAGE = "📭 Todavía no tenés gastos registrados, así que no hay nada para reportar."
COOLDOWN_MESSAGE = "⏳ Ya te generé un reporte hace poco. Revisá tu mail o tu Drive, o pedime uno nuevo en unos minutos."
REPORT_ERROR_MESSAGE = "⚠️ No pude generar tu reporte en este momento. Por favor intentá de nuevo en unos minutos."


def _now_label() -> str:
    try:
        now = datetime.now(ZoneInfo(settings.TIMEZONE))
    except Exception:
        now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M")


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


class ReportService:
    """Builds the report of ONE user: rows are selected by the SHA-256 of the requester's own phone number only."""

    def __init__(
        self,
        expense_repository: ExpenseRepositoryInterface,
        user_repository: UserRepositoryInterface,
        publisher: ReportPublisherInterface,
        cooldown: Optional[TTLCache] = None,
    ):
        self.expense_repository = expense_repository
        self.user_repository = user_repository
        self.publisher = publisher
        self._cooldown: TTLCache = cooldown or TTLCache(settings.REPORT_COOLDOWN_SECONDS or 1)
        self._cooldown_enabled = settings.REPORT_COOLDOWN_SECONDS > 0 or cooldown is not None

    async def handle(self, phone: str) -> str:
        """Generate and share the report of the sender. Returns the message to send back."""
        user = await asyncio.to_thread(self.user_repository.get_user, phone)
        email = user.email if user else None
        if not email:
            return NO_EMAIL_MESSAGE

        phone_hash = DataQualityGate.hash_phone_number(phone)
        # Every report creates a new file in Drive: one at a time per user
        if self._cooldown_enabled and not self._cooldown.set_if_absent(phone_hash, True):
            return COOLDOWN_MESSAGE

        try:
            rows = await asyncio.to_thread(self.expense_repository.get_expenses_by_user, phone_hash)
            if not rows:
                self._cooldown.delete(phone_hash)
                return NO_EXPENSES_MESSAGE

            title = f"Reporte de gastos {_now_label()} - {phone_hash[:8]}"
            url = await asyncio.to_thread(self.publisher.publish, title, rows, email)
        except Exception as e:
            logger.error(f"Report generation failed for {mask_phone(phone)}: {e}")
            self._cooldown.delete(phone_hash)  # let the user retry right away
            return REPORT_ERROR_MESSAGE

        return (
            f"📊 *Tu reporte de gastos está listo*\n\n"
            f"🧾 *Gastos:* {len(rows)}\n"
            f"💰 *Totales:* {self._totals(rows)}\n\n"
            f"🔗 {url}\n\n"
            f"Lo compartí en modo solo lectura con {_mask_email(email)}. "
            f"Abrilo con esa cuenta de Google."
        )

    @staticmethod
    def _totals(rows: list[dict]) -> str:
        totals: dict[str, float] = defaultdict(float)
        for row in rows:
            try:
                totals[str(row.get("moneda") or "")] += float(row.get("monto") or 0)
            except (TypeError, ValueError):
                continue
        return " · ".join(f"{currency} {amount:,.2f}" for currency, amount in sorted(totals.items())) or "—"
