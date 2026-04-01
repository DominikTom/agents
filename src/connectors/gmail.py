"""Gmail connector - fetches recent emails using Google Gmail API."""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from src.common.config import get_env
from src.common.types import ConnectorResult
from src.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailConnector(BaseConnector):
    name = "gmail"

    def __init__(self, config: dict):
        super().__init__(config)
        self._service = None

    def _get_service(self):
        if self._service is None:
            creds = Credentials.from_service_account_file(
                get_env("GOOGLE_CREDENTIALS_PATH"),
                scopes=SCOPES,
            )
            # Delegate to user's mailbox
            delegated = creds.with_subject(get_env("GMAIL_USER_EMAIL"))
            self._service = build("gmail", "v1", credentials=delegated)
        return self._service

    async def fetch(self, since: datetime | None = None) -> ConnectorResult:
        try:
            service = self._get_service()

            if since is None:
                lookback = self.config.get("connectors", {}).get("gmail", {})
                hours = lookback.get("since_hours", 13)  # default: 18:00 previous day
                since = datetime.now() - timedelta(hours=hours)

            # Gmail API uses epoch seconds for after: query
            after_epoch = int(since.timestamp())
            query = f"after:{after_epoch}"

            results = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=50)
                .execute()
            )

            messages = results.get("messages", [])
            items = []

            for msg_ref in messages:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_ref["id"], format="metadata",
                         metadataHeaders=["From", "Subject", "Date"])
                    .execute()
                )

                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                snippet = msg.get("snippet", "")

                items.append({
                    "id": msg["id"],
                    "from": headers.get("From", ""),
                    "subject": headers.get("Subject", ""),
                    "date": headers.get("Date", ""),
                    "snippet": snippet,
                    "labels": msg.get("labelIds", []),
                })

            logger.info(f"Gmail: fetched {len(items)} emails since {since}")
            return self._success_result(items)

        except Exception as e:
            logger.error(f"Gmail fetch failed: {e}")
            return self._error_result(str(e))
