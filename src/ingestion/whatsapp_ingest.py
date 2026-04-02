"""WhatsApp message ingestion - syncs messages from bridge to PostgreSQL events table."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

import httpx

from src.storage.database import Database

logger = logging.getLogger(__name__)


class WhatsAppIngestor:
    """Pulls messages from whatsapp-bridge and stores them as events in PostgreSQL."""

    def __init__(self, db: Database, bridge_url: str):
        self.db = db
        self.bridge_url = bridge_url.rstrip("/")
        # Track last synced timestamp per JID to avoid re-fetching
        self._last_ts: dict[str, int] = {}

    async def sync(self) -> dict:
        """Sync all chats from bridge to DB. Returns stats dict."""
        stats = {"chats_synced": 0, "new_messages": 0, "errors": 0}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Get all chats
                resp = await client.get(f"{self.bridge_url}/chats")
                resp.raise_for_status()
                chats = resp.json().get("chats", [])

                for chat in chats:
                    if chat.get("messageCount", 0) == 0:
                        continue

                    chat_name = chat["name"]
                    jid = chat["id"]
                    is_group = chat.get("isGroup", False)

                    try:
                        new = await self._sync_chat(
                            client, jid, chat_name, is_group
                        )
                        if new > 0:
                            stats["chats_synced"] += 1
                            stats["new_messages"] += new
                    except Exception as e:
                        logger.warning(f"WhatsApp sync error for {chat_name}: {e}")
                        stats["errors"] += 1

        except httpx.HTTPError as e:
            logger.error(f"WhatsApp bridge unreachable: {e}")
            stats["errors"] += 1

        if stats["new_messages"] > 0:
            logger.info(
                f"WhatsApp sync: {stats['new_messages']} new messages "
                f"from {stats['chats_synced']} chats"
            )

        return stats

    async def _sync_chat(
        self,
        client: httpx.AsyncClient,
        jid: str,
        chat_name: str,
        is_group: bool,
    ) -> int:
        """Sync a single chat. Returns count of new messages stored."""
        since_ts = self._last_ts.get(jid, 0)

        # Fetch messages from bridge
        param_key = "chat" if is_group else "contact"
        resp = await client.get(
            f"{self.bridge_url}/messages",
            params={param_key: chat_name, "since": since_ts},
        )
        resp.raise_for_status()
        messages = resp.json().get("messages", [])

        if not messages:
            return 0

        new_count = 0
        max_ts = since_ts

        for msg in messages:
            msg_id = msg.get("id", "")
            msg_ts = int(msg.get("timestamp", 0))
            body = msg.get("body", "")
            sender = msg.get("from", "")
            has_media = msg.get("hasMedia", False)
            from_me = msg.get("fromMe", False)

            if not body and not has_media:
                continue

            # Skip messages we already have (by timestamp)
            if msg_ts <= since_ts:
                continue

            source_id = f"wa:{jid}:{msg_id}" if msg_id else f"wa:{jid}:{msg_ts}:{_hash(body)}"
            timestamp = datetime.fromtimestamp(msg_ts, tz=timezone.utc)

            # Try to resolve sender entity
            sender_entity_id = None
            if sender and not from_me:
                sender_entity_id = await self.db.resolve_entity("whatsapp", sender)

            event_id = await self.db.store_event(
                source="whatsapp",
                source_id=source_id,
                event_type="message",
                timestamp=timestamp,
                title=chat_name,
                body=body if body else "[media]",
                sender_entity_id=sender_entity_id,
                category="group" if is_group else "direct",
                metadata={
                    "chat_jid": jid,
                    "chat_name": chat_name,
                    "sender_name": sender,
                    "from_me": from_me,
                    "has_media": has_media,
                    "is_group": is_group,
                },
            )

            if event_id is not None:
                new_count += 1

            if msg_ts > max_ts:
                max_ts = msg_ts

        # Update last sync timestamp
        if max_ts > since_ts:
            self._last_ts[jid] = max_ts

        return new_count


def _hash(text: str) -> str:
    """Short hash for dedup when message ID is missing."""
    return hashlib.md5(text.encode()).hexdigest()[:8]
