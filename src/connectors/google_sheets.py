"""Google Sheets connector - reads data from configured spreadsheets."""

from __future__ import annotations

import logging
from datetime import datetime

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from src.common.config import get_env
from src.common.types import ConnectorResult
from src.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class GoogleSheetsConnector(BaseConnector):
    name = "google_sheets"

    def __init__(self, config: dict):
        super().__init__(config)
        self._service = None

    def _get_service(self):
        if self._service is None:
            creds = Credentials.from_service_account_file(
                get_env("GOOGLE_CREDENTIALS_PATH"),
                scopes=SCOPES,
            )
            self._service = build("sheets", "v4", credentials=creds)
        return self._service

    async def fetch(self, since: datetime | None = None) -> ConnectorResult:
        try:
            service = self._get_service()
            sheets_config = self.config.get("connectors", {}).get("google_sheets", {})
            spreadsheets = sheets_config.get("spreadsheets", [])

            if not spreadsheets:
                return self._success_result([])

            items = []

            for sheet_conf in spreadsheets:
                spreadsheet_id = sheet_conf.get("id", "")
                sheet_name = sheet_conf.get("sheet", "Sheet1")
                range_str = sheet_conf.get("range", f"{sheet_name}!A1:Z100")
                label = sheet_conf.get("label", spreadsheet_id[:8])

                result = (
                    service.spreadsheets()
                    .values()
                    .get(spreadsheetId=spreadsheet_id, range=range_str)
                    .execute()
                )

                values = result.get("values", [])
                if not values:
                    continue

                # First row as headers
                headers = values[0]
                for row in values[1:]:
                    row_dict = {}
                    for i, header in enumerate(headers):
                        row_dict[header] = row[i] if i < len(row) else ""
                    items.append({
                        "sheet": label,
                        "data": row_dict,
                    })

            logger.info(f"Google Sheets: fetched {len(items)} rows from {len(spreadsheets)} sheets")
            return self._success_result(items)

        except Exception as e:
            logger.error(f"Google Sheets fetch failed: {e}")
            return self._error_result(str(e))
