"""Google Calendar connector - fetches today's events."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from src.common.config import get_env
from src.common.types import ConnectorResult
from src.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class GoogleCalendarConnector(BaseConnector):
    name = "google_calendar"

    def __init__(self, config: dict):
        super().__init__(config)
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

    async def fetch(self, since: datetime | None = None) -> ConnectorResult:
        try:
            service = self._get_service()
            cal_config = self.config.get("connectors", {}).get("google_calendar", {})
            calendar_ids = cal_config.get("calendar_ids", ["primary"])

            # Today's events
            now = datetime.now()
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_day = start_of_day + timedelta(days=1)

            time_min = start_of_day.isoformat() + "Z"
            time_max = end_of_day.isoformat() + "Z"

            items = []

            for cal_id in calendar_ids:
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
                    start = event["start"].get("dateTime", event["start"].get("date", ""))
                    end = event["end"].get("dateTime", event["end"].get("date", ""))

                    attendees = [
                        a.get("email", "")
                        for a in event.get("attendees", [])
                        if a.get("responseStatus") != "declined"
                    ]

                    items.append({
                        "title": event.get("summary", "(no title)"),
                        "start": start,
                        "end": end,
                        "location": event.get("location", ""),
                        "attendees": attendees,
                        "link": event.get("hangoutLink", ""),
                        "calendar": cal_id,
                    })

            logger.info(f"Google Calendar: fetched {len(items)} events for today")
            return self._success_result(items)

        except Exception as e:
            logger.error(f"Google Calendar fetch failed: {e}")
            return self._error_result(str(e))
