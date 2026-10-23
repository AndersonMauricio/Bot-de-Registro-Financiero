"""Publishes a user's expense report as a new Google Spreadsheet shared read-only with their email."""

from gspread.utils import ValueInputOption
from src.core.config import settings
from src.core.exceptions import StorageError
from src.core.logging import logger
from src.infrastructure.storage.base import ReportPublisherInterface
from src.infrastructure.storage.sheets_client import get_gspread_client
from src.infrastructure.storage.sheets_repo import REPORT_COLUMNS

REPORT_HEADERS = ["Fecha del gasto", "Monto", "Moneda", "Categoría", "Subcategoría", "Medio de pago", "Descripción"]
REPORT_WORKSHEET_TITLE = "Gastos"


class GoogleSheetsReportPublisher(ReportPublisherInterface):
    """Creates the spreadsheet with the service account (in `REPORTS_FOLDER_ID` when configured)."""

    def publish(self, title: str, rows: list[dict], share_with_email: str) -> str:
        client = get_gspread_client()
        spreadsheet = None
        try:
            spreadsheet = client.create(title, folder_id=settings.REPORTS_FOLDER_ID or None)
            worksheet = spreadsheet.sheet1
            values = [REPORT_HEADERS] + [[row.get(column, "") for column in REPORT_COLUMNS] for row in rows]
            worksheet.update_title(REPORT_WORKSHEET_TITLE)
            worksheet.resize(rows=max(len(values), 2), cols=len(REPORT_HEADERS))
            # RAW: nothing in the report is ever interpreted as a formula
            worksheet.update(values=values, range_name="A1", value_input_option=ValueInputOption.raw)
            worksheet.freeze(rows=1)
            worksheet.format(f"A1:{chr(64 + len(REPORT_HEADERS))}1", {"textFormat": {"bold": True}})
            spreadsheet.share(share_with_email, perm_type="user", role="reader")
        except Exception as e:
            logger.error(f"Failed to publish the expense report: {e}")
            if spreadsheet is not None:
                try:  # do not leave a half-built (possibly shared) file behind
                    client.del_spreadsheet(spreadsheet.id)
                except Exception as cleanup_error:
                    logger.warning(f"Could not delete the incomplete report: {cleanup_error}")
            raise StorageError(f"Report publication failed: {e}") from e
        logger.info(f"Expense report with {len(rows)} rows published and shared (read-only).")
        return spreadsheet.url
