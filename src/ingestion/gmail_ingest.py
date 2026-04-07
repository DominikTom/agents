"""Gmail message ingestion - syncs emails to PostgreSQL events table."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from src.common.config import get_env
from src.storage.database import Database

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailIngestor:
    """Pulls emails from Gmail API and stores them as events in PostgreSQL."""

    def __init__(self, db: Database):
        self.db = db
        self._service = None

    def _get_service(self):
        if self._service is None:
            creds = Credentials.from_service_account_file(
                get_env("GOOGLE_CREDENTIALS_PATH"),
                scopes=SCOPES,
            )
            delegated = creds.with_subject(get_env("GMAIL_USER_EMAIL"))
            self._service = build("gmail", "v1", credentials=delegated)
        return self._service

    async def sync(self, lookback_hours: int = 1) -> dict:
        """Sync recent emails to DB. Returns stats."""
        stats = {"new_emails": 0, "errors": 0}

        try:
            service = self._get_service()
            since = datetime.now() - timedelta(hours=lookback_hours)
            after_epoch = int(since.timestamp())

            results = (
                service.users()
                .messages()
                .list(userId="me", q=f"after:{after_epoch}", maxResults=50)
                .execute()
            )

            for msg_ref in results.get("messages", []):
                try:
                    msg = (
                        service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=msg_ref["id"],
                            format="metadata",
                            metadataHeaders=["From", "Subject", "Date"],
                        )
                        .execute()
                    )

                    headers = {
                        h["name"]: h["value"]
                        for h in msg.get("payload", {}).get("headers", [])
                    }
                    sender = headers.get("From", "")
                    subject = headers.get("Subject", "")
                    snippet = msg.get("snippet", "")
                    labels = msg.get("labelIds", [])
                    internal_date = int(msg.get("internalDate", 0)) / 1000

                    timestamp = datetime.fromtimestamp(internal_date, tz=timezone.utc)

                    # Extract sender name for entity resolution
                    sender_name = _extract_name(sender)
                    sender_entity_id = None
                    if sender_name:
                        sender_entity_id = await self.db.resolve_entity("gmail", sender)
                        if not sender_entity_id:
                            sender_entity_id = await self.db.resolve_entity("gmail", sender_name)

                    event_id = await self.db.store_event(
                        source="gmail",
                        source_id=f"gmail:{msg_ref['id']}",
                        event_type="email",
                        timestamp=timestamp,
                        title=subject,
                        body=snippet,
                        sender_entity_id=sender_entity_id,
                        priority=_classify_priority(labels),
                        category="inbox",
                        metadata={
                            "from": sender,
                            "sender_name": sender_name,
                            "labels": labels,
                            "gmail_id": msg_ref["id"],
                        },
                    )

                    if event_id is not None:
                        stats["new_emails"] += 1

                except Exception as e:
                    logger.warning(f"Gmail ingest error for {msg_ref['id']}: {e}")
                    stats["errors"] += 1

        except Exception as e:
            logger.error(f"Gmail ingestion failed: {e}")
            stats["errors"] += 1

        if stats["new_emails"] > 0:
            logger.info(f"Gmail sync: {stats['new_emails']} new emails ingested")

        return stats


def _extract_name(from_header: str) -> str:
    """Extract display name from 'Name <email>' format."""
    if "<" in from_header:
        name = from_header.split("<")[0].strip().strip('"')
        return name
    return from_header.split("@")[0] if "@" in from_header else from_header


def _classify_priority(labels: list[str]) -> str:
    """Classify email priority from Gmail labels."""
    if "IMPORTANT" in labels and "UNREAD" in labels:
        return "high"
    if "IMPORTANT" in labels:
        return "normal"
    if "SPAM" in labels or "TRASH" in labels:
        return "low"
    return "normal"
