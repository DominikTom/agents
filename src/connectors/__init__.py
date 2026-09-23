"""Connector factory for live (non-ingested) sources."""

from __future__ import annotations

from src.connectors.base import BaseConnector


def create_connector(name: str, config: dict) -> BaseConnector | None:
    from src.connectors.gmail import GmailConnector
    from src.connectors.google_calendar import GoogleCalendarConnector
    from src.connectors.ideaerp import IdeaERPConnector
    from src.connectors.slack_reader import SlackReaderConnector

    connectors = {
        "gmail": GmailConnector,
        "slack": SlackReaderConnector,
        "google_calendar": GoogleCalendarConnector,
        "ideaerp": IdeaERPConnector,
    }
    cls = connectors.get(name)
    return cls(config) if cls else None
