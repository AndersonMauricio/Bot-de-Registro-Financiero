"""CLI test script to verify Google Sheets connection and write a test expense."""

import sys
import os
sys.path.insert(0, os.path.abspath("."))

from datetime import date, time
from uuid import uuid4
from src.core.config import settings
from src.core.logging import logger
from src.domain.enums import InputTypeEnum
from src.domain.models import EXPENSE_SHEET_HEADERS, ExpenseRecord
from src.infrastructure.storage.sheets_repo import GoogleSheetsExpenseRepository


def test_write():
    print("=" * 60)
    print("🔍 VERIFICANDO CONEXIÓN A GOOGLE SHEETS...")
    print(f"📄 Spreadsheet ID: {settings.SPREADSHEET_ID}")
    print(f"📑 Tab / Pestaña:   {settings.WORKSHEET_NAME}")
    print("=" * 60)

    repo = GoogleSheetsExpenseRepository()

    sample_record = ExpenseRecord(
        transaction_id=uuid4(),
        message_id=f"test_cli_{uuid4().hex[:8]}",
        user_phone_hash="0" * 64,  # fictitious hash: no real phone number is involved
        input_type=InputTypeEnum.TEXT,
        transaction_date=date.today(),
        transaction_time=time(12, 0, 0),
        amount=99.99,
        currency="USD",
        category="Alimentación",
        subcategory="Prueba Técnica",
        merchant_name="Test Store",
        payment_method="Tarjeta de Crédito",
        description="Fila de prueba escrita desde el script de verificación",
        confidence_score=0.99,
        raw_text="Prueba de inserción en Google Sheets",
    )

    try:
        assigned_id = repo.save_expense(sample_record)
        sheet_row = sample_record.to_sheets_row()
        print("\n✅ ¡ÉXITO TOTAL!")
        print(f"👉 Fila insertada correctamente con las {len(sheet_row)} columnas del esquema:")
        for position, (column, value) in enumerate(zip(EXPENSE_SHEET_HEADERS, sheet_row), start=1):
            print(f"   {position:>2}. {column:<16} {value}")
        print(f"\nRevisa tu hoja de cálculo en la pestaña '{settings.WORKSHEET_NAME}'.")
    except Exception as e:
        print(f"\n❌ ERROR AL ESCRIBIR EN GOOGLE SHEETS:\n{e}")
        print("\n🛠️ Posibles causas:")
        print("1. Falta el archivo .json de credenciales de Google Cloud en 'config/credentials/'")
        print(f"   (o la fila 1 de la pestaña no tiene los encabezados: {', '.join(EXPENSE_SHEET_HEADERS)})")
        print(f"2. La hoja no está compartida con permiso de 'Editor' al email: {settings.GOOGLE_APPLICATION_CREDENTIALS or 'de tu Service Account'}")
        print(f"3. La pestaña '{settings.WORKSHEET_NAME}' no existe dentro del documento.")


if __name__ == "__main__":
    test_write()
