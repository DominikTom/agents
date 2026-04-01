"""Connector factory."""

from __future__ import annotations

from src.connectors.base import BaseConnector


def create_connector(name: str, config: dict) -> BaseConnector | None:
    """Create a connector by name."""
    from src.connectors.asana import AsanaConnector
    from src.connectors.gmail import GmailConnector
    from src.connectors.google_calendar import GoogleCalendarConnector
    from src.connectors.google_sheets import GoogleSheetsConnector
    from src.connectors.shoper import ShoperConnector
    from src.connectors.shopify import ShopifyConnector
    from src.connectors.slack_reader import SlackReaderConnector
    from src.connectors.whatsapp import WhatsAppConnector

    connectors = {
        "gmail": GmailConnector,
        "asana": AsanaConnector,
        "slack": SlackReaderConnector,
        "google_calendar": GoogleCalendarConnector,
        "google_sheets": GoogleSheetsConnector,
        "shoper": ShoperConnector,
        "shopify": ShopifyConnector,
        "whatsapp": WhatsAppConnector,
    }

    cls = connectors.get(name)
    if cls is None:
        return None
    return cls(config)
