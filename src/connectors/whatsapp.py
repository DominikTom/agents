"""WhatsApp connector - reads messages via whatsapp-web.js bridge HTTP API."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from src.common.config import get_env
from src.common.types import ConnectorResult
from src.connectors.base import BaseConnector

logger = logging.getLogger(__name__)


class WhatsAppConnector(BaseConnector):
    name = "whatsapp"

    async def fetch(self, since: datetime | None = None) -> ConnectorResult:
        try:
            bridge_url = get_env("WHATSAPP_BRIDGE_URL")
            wa_config = self.config.get("connectors", {}).get("whatsapp", {})
            chats = wa_config.get("chats", [])
            contacts = wa_config.get("contacts", [])

            if not chats and not contacts:
                return self._success_result([])

            if since is None:
                since = datetime.now() - timedelta(hours=13)

            since_ts = int(since.timestamp())
            items = []

            async with httpx.AsyncClient(timeout=30.0) as client:
                # Fetch messages from configured chats
                for chat_name in chats:
                    try:
                        response = await client.get(
                            f"{bridge_url}/messages",
                            params={"chat": chat_name, "since": since_ts},
                        )
                        response.raise_for_status()
                        messages = response.json().get("messages", [])

                        for msg in messages:
                            items.append({
                                "chat": chat_name,
                                "chat_type": "group",
                                "from": msg.get("from", ""),
                                "body": msg.get("body", ""),
                                "timestamp": msg.get("timestamp", ""),
                                "has_media": msg.get("hasMedia", False),
                            })
                    except httpx.HTTPError as e:
                        logger.warning(f"WhatsApp: failed to fetch chat '{chat_name}': {e}")

                # Fetch messages from key contacts (1:1)
                for contact in contacts:
                    try:
                        response = await client.get(
                            f"{bridge_url}/messages",
                            params={"contact": contact, "since": since_ts},
                        )
                        response.raise_for_status()
                        messages = response.json().get("messages", [])

                        for msg in messages:
                            items.append({
                                "chat": f"DM:{contact}",
                                "chat_type": "direct",
                                "from": msg.get("from", ""),
                                "body": msg.get("body", ""),
                                "timestamp": msg.get("timestamp", ""),
                                "has_media": msg.get("hasMedia", False),
                            })
                    except httpx.HTTPError as e:
                        logger.warning(f"WhatsApp: failed to fetch contact '{contact}': {e}")

            logger.info(f"WhatsApp: fetched {len(items)} messages")
            return self._success_result(items)

        except Exception as e:
            logger.error(f"WhatsApp fetch failed: {e}")
            return self._error_result(str(e))
