"""WhatsApp ingestion — copies new messages from the bridge buffer to the events table.

Chats are addressed by JID (never by name, which is ambiguous), and every chat
resumes from the newest message already stored, so restarts and long bridge
outages don't lose or duplicate anything.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from src.connectors.whatsapp_bridge import BridgeClient, BridgeError
from src.storage.database import Database

logger = logging.getLogger(__name__)

MEDIA_LABELS = {
    "image": "zdjęcie",
    "video": "wideo",
    "document": "dokument",
    "sticker": "naklejka",
    "location": "lokalizacja",
    "contact": "kontakt",
    "poll": "ankieta",
}


def render_body(msg: dict) -> str:
    """Human/AI-readable body, including a marker for non-text content."""
    body = (msg.get("body") or "").strip()
    kind = msg.get("type") or ("media" if msg.get("hasMedia") else "text")
    if kind == "audio":
        secs = int(msg.get("durationSec") or 0)
        label = "głosówka" if msg.get("isVoiceNote", True) else "audio"
        return f"[{label} {secs // 60}:{secs % 60:02d}]"
    if kind in MEDIA_LABELS:
        label = MEDIA_LABELS[kind]
        if kind == "document" and msg.get("fileName"):
            label = f"dokument: {msg['fileName']}"
        return f"[{label}] {body}".strip() if body and body != msg.get("fileName") else f"[{label}]"
    if kind == "media" and not body:
        return "[media]"
    return body


class WhatsAppIngestor:
    def __init__(self, db: Database, bridge_url: str | None = None, bridge: BridgeClient | None = None):
        self.db = db
        self.bridge = bridge or BridgeClient(url=bridge_url)

    async def sync(self) -> dict:
        stats = {"chats_synced": 0, "new_messages": 0, "errors": 0}
        try:
            chats = await self.bridge.chats()
        except BridgeError as e:
            logger.error(f"WhatsApp bridge unreachable: {e}")
            stats["errors"] += 1
            stats["error"] = str(e)
            return stats

        last_ts = await self.db.get_whatsapp_last_ts()
        disabled = await self.db.get_disabled_chat_jids()

        for chat in chats:
            jid = chat["id"]
            name = chat.get("name") or jid.split("@")[0]
            is_group = bool(chat.get("isGroup"))
            await self.db.upsert_whatsapp_chat(jid, name, is_group)
            if jid in disabled or not chat.get("messageCount"):
                continue
            newest = chat.get("lastMessageAt") or 0
            since = last_ts.get(jid, 0)
            if newest and newest <= since:
                continue
            try:
                new = await self._sync_chat(jid, name, is_group, since)
            except BridgeError as e:
                logger.warning(f"WhatsApp sync error for {name}: {e}")
                stats["errors"] += 1
                continue
            if new:
                stats["chats_synced"] += 1
                stats["new_messages"] += new

        if stats["new_messages"]:
            logger.info(f"WhatsApp sync: {stats['new_messages']} new messages from {stats['chats_synced']} chats")
        return stats

    async def _sync_chat(self, jid: str, chat_name: str, is_group: bool, since: int) -> int:
        messages = await self.bridge.messages(jid, since=since)
        new_count = 0
        for msg in messages:
            ts = int(msg.get("timestamp") or 0)
            if ts < since:
                continue
            body = render_body(msg)
            if not body:
                continue
            msg_id = msg.get("id") or ""
            source_id = f"wa:{jid}:{msg_id}" if msg_id else f"wa:{jid}:{ts}:{_hash(body)}"
            sender = msg.get("from") or ""
            from_me = bool(msg.get("fromMe"))

            sender_entity_id = None
            if sender and not from_me:
                sender_entity_id = await self.db.resolve_entity("whatsapp", sender)

            quoted = msg.get("quoted") or None
            event_id = await self.db.store_event(
                source="whatsapp",
                source_id=source_id,
                event_type="message",
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                title=chat_name,
                body=body,
                sender_entity_id=sender_entity_id,
                priority="high" if msg.get("mentionsMe") else "normal",
                category="group" if is_group else "direct",
                metadata={
                    "chat_jid": jid,
                    "chat_name": chat_name,
                    "sender_name": sender,
                    "sender_jid": msg.get("participant") or ("" if is_group else jid),
                    "from_me": from_me,
                    "has_media": bool(msg.get("hasMedia")),
                    "msg_type": msg.get("type") or "text",
                    "duration_sec": msg.get("durationSec"),
                    "file_name": msg.get("fileName"),
                    "is_group": is_group,
                    "mentions_me": bool(msg.get("mentionsMe")),
                    "forwarded": bool(msg.get("forwarded")),
                    "edited": bool(msg.get("edited")),
                    "revoked": bool(msg.get("revoked")),
                    "wa_id": msg_id,
                    "reply_to": quoted.get("id") if quoted else None,
                    "quoted_text": (quoted.get("text") or "")[:300] if quoted else None,
                },
            )
            if event_id is not None:
                new_count += 1
        return new_count


def _hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:8]
