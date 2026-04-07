"""Google Calendar ingestion - syncs calendar events to PostgreSQL events table."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from src.common.config import get_env
from src.storage.database import Database

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class CalendarIngestor:
    """Pulls calendar events and stores them in PostgreSQL events table."""

    def __init__(self, db: Database, config: dict):
        self.db = db
        self.config = config
        self._service = None

    def _get_service(self):
        if self._service is None:
            creds = Credentials.from_service_account_file(
                get_env("GOOGLE_CREDENTIALS_PATH"),
                scopes=SCOPES,
            )
            delegated = creds.with_subject(get_env("GMAIL_USER_EMAIL"))
            self._service = build("calendar", "v3", credentials=delegated)
        return self._service

    async def sync(self, days_ahead: int = 2) -> dict:
        """Sync calendar events (today + tomorrow) to DB. Returns stats."""
        stats = {"new_events": 0, "errors": 0}

        try:
            service = self._get_service()
            cal_config = self.config.get("connectors", {}).get("google_calendar", {})
            calendar_ids = cal_config.get("calendar_ids", ["primary"])

            now = datetime.now()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=days_ahead)

            time_min = start.isoformat() + "Z"
            time_max = end.isoformat() + "Z"

            for cal_id in calendar_ids:
                try:
                    events_result = (
                        service.events()
                        .list(
                            calendarId=cal_id,
                            timeMin=time_min,
                            timeMax=time_max,
                            singleEvents=True,
                            orderBy="startTime",
                        )
                        .execute()
                    )

                    for event in events_result.get("items", []):
                        event_id_str = event.get("id", "")
                        title = event.get("summary", "(no title)")
                        start_str = event["start"].get("dateTime", event["start"].get("date", ""))
                        end_str = event["end"].get("dateTime", event["end"].get("date", ""))
                        location = event.get("location", "")
                        attendees = [
                            a.get("email", "")
                            for a in event.get("attendees", [])
                            if a.get("responseStatus") != "declined"
                        ]
                        link = event.get("hangoutLink", "")

                        # Parse start time
                        timestamp = _parse_datetime(start_str)

                        stored_id = await self.db.store_event(
                            source="calendar",
                            source_id=f"cal:{cal_id}:{event_id_str}",
                            event_type="meeting",
                            timestamp=timestamp,
                            title=title,
                            body=f"{start_str} - {end_str}" + (f" | {location}" if location else ""),
                            priority="normal",
                            category="calendar",
                            metadata={
                                "calendar_id": cal_id,
                                "google_event_id": event_id_str,
                                "start": start_str,
                                "end": end_str,
                                "location": location,
                                "attendees": attendees,
                                "hangout_link": link,
                            },
                        )

                        if stored_id is not None:
                            stats["new_events"] += 1

                except Exception as e:
                    logger.warning(f"Calendar ingest error for {cal_id}: {e}")
                    stats["errors"] += 1

        except Exception as e:
            logger.error(f"Calendar ingestion failed: {e}")
            stats["errors"] += 1

        if stats["new_events"] > 0:
            logger.info(f"Calendar sync: {stats['new_events']} new events ingested")

        return stats


def _parse_datetime(dt_str: str) -> datetime:
    """Parse Google Calendar datetime string to timezone-aware datetime."""
    try:
        if "T" in dt_str:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return datetime.fromisoformat(dt_str + "T00:00:00+00:00")
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)
