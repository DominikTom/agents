"""Google Calendar ingestion - syncs calendar events to PostgreSQL events table."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from src.common.config import get_env
from src.common.timeutil import day_start, today
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

    async def sync(self, days_ahead: int = 8) -> dict:
        """Sync calendar events (today + tomorrow) to DB. Returns stats."""
        stats = {"new_events": 0, "errors": 0}

        try:
            service = self._get_service()
            cal_config = self.config.get("connectors", {}).get("google_calendar", {})
            calendar_ids = cal_config.get("calendar_ids", ["primary"])

            start = day_start(today())
            end = start + timedelta(days=days_ahead)
            time_min = start.isoformat()
            time_max = end.isoformat()
            seen: set[str] = set()

            for cal_id in calendar_ids:
                try:
                    events_result = await asyncio.to_thread(
                        lambda: service.events().list(
                            calendarId=cal_id,
                            timeMin=time_min,
                            timeMax=time_max,
                            singleEvents=True,
                            orderBy="startTime",
                            maxResults=250,
                        ).execute()
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
                        description = event.get("description", "")

                        # Parse start time
                        timestamp = _parse_datetime(start_str)

                        # Build rich body: time + location + agenda
                        body_parts = [f"{start_str} - {end_str}"]
                        if location:
                            body_parts.append(f"Location: {location}")
                        if description:
                            body_parts.append(f"Agenda: {description[:1000]}")

                        source_id = f"cal:{cal_id}:{event_id_str}"
                        seen.add(source_id)
                        stored_id = await self.db.store_event(
                            upsert=True,
                            source="calendar",
                            source_id=source_id,
                            event_type="meeting",
                            timestamp=timestamp,
                            title=title,
                            body="\n".join(body_parts),
                            priority="normal",
                            category="calendar",
                            metadata={
                                "calendar_id": cal_id,
                                "google_event_id": event_id_str,
                                "start": start_str,
                                "end": end_str,
                                "location": location,
                                "description": description[:1000] if description else "",
                                "attendees": attendees,
                                "hangout_link": link,
                                "all_day": "T" not in start_str,
                                "organizer": (event.get("organizer") or {}).get("email", ""),
                                "my_status": next(
                                    (a.get("responseStatus") for a in event.get("attendees", []) if a.get("self")),
                                    None,
                                ),
                            },
                        )

                        if stored_id is not None:
                            stats["new_events"] += 1

                except Exception as e:
                    logger.warning(f"Calendar ingest error for {cal_id}: {e}")
                    stats["errors"] += 1

            if not stats["errors"]:
                await self.db.prune_calendar_window(start, end, seen)

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
        return day_start(datetime.fromisoformat(dt_str).date())
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)
