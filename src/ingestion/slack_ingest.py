"""Slack → events. Reads the channels the bot is in and that are switched on in Źródła → Slack.

Channel settings live in the `settings` table (key "slack"):
    {"channels": {"C123": {"name": "dev-daily-updates", "read": true, "daily": true}}}
New channels get defaults from their name: daily-report channels (daily/update/case…) are read
and treated as team daily reports; everything else is off until switched on in the panel.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone

from src.connectors.slack_api import DAILY_NAME, SlackAPI, SlackError
from src.storage.database import Database

logger = logging.getLogger(__name__)

FIRST_RUN_DAYS = 7          # history loaded the first time a channel is read
RECHECK_DAYS = 2            # threads and edits re-read on every run
SKIP_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
                 "bot_message", "bot_add", "bot_remove", "pinned_item", "unpinned_item"}


async def load_channel_settings(db: Database) -> dict:
    return (await db.get_setting("slack", {}) or {}).get("channels", {}) or {}


async def refresh_channels(db: Database, api: SlackAPI) -> dict:
    """Merge the bot's current channels into the stored settings (defaults for new ones)."""
    stored = await db.get_setting("slack", {}) or {}
    channels = dict(stored.get("channels") or {})
    member = await api.member_channels()
    member_ids = set()
    for ch in member:
        member_ids.add(ch["id"])
        cur = channels.get(ch["id"])
        if cur is None:
            daily = bool(DAILY_NAME.search(ch["name"]))
            channels[ch["id"]] = {"name": ch["name"], "private": ch["private"], "read": daily, "daily": daily}
        else:
            cur.update(name=ch["name"], private=ch["private"])
    for cid, cfg in channels.items():
        cfg["member"] = cid in member_ids
    if channels != stored.get("channels"):
        await db.set_setting("slack", {**stored, "channels": channels})
    return channels


class SlackIngestor:
    def __init__(self, db: Database, api: SlackAPI | None = None):
        self.db = db
        self.api = api or SlackAPI()
        self.bot_user_id: str | None = None

    async def sync(self) -> dict:
        if not self.api.configured:
            return {"skipped": "SLACK_BOT_TOKEN not set"}
        stats = {"channels": 0, "new_messages": 0, "errors": []}
        try:
            self.bot_user_id = (await self.api.identity()).get("bot_id")
            channels = await refresh_channels(self.db, self.api)
        except SlackError as e:
            return {**stats, "error": str(e)}
        last = await self.db.get_slack_last_ts()
        now = time.time()
        for cid, cfg in channels.items():
            if not cfg.get("read") or not cfg.get("member"):
                continue
            first = now - FIRST_RUN_DAYS * 86400
            oldest = min(float(last.get(cid) or first), now - RECHECK_DAYS * 86400)
            try:
                stats["new_messages"] += await self._sync_channel(cid, cfg, f"{oldest:.6f}", now)
                stats["channels"] += 1
            except SlackError as e:
                logger.warning(f"Slack #{cfg.get('name')}: {e}")
                stats["errors"].append(f"#{cfg.get('name')}: {e}")
        if stats["errors"] and not stats["channels"]:
            stats["error"] = stats["errors"][0]
        if stats["new_messages"]:
            logger.info(f"Slack sync: {stats['new_messages']} new messages from {stats['channels']} channels")
        return stats

    async def _sync_channel(self, cid: str, cfg: dict, oldest: str, now: float) -> int:
        new = 0
        for msg in await self.api.history(cid, oldest):
            if await self._store(cid, cfg, msg):
                new += 1
            latest_reply = float(msg.get("latest_reply") or 0)
            if msg.get("reply_count") and latest_reply > now - RECHECK_DAYS * 86400:
                for reply in await self.api.replies(cid, msg["ts"]):
                    if await self._store(cid, cfg, reply, thread_ts=msg["ts"]):
                        new += 1
        return new

    async def _store(self, cid: str, cfg: dict, msg: dict, thread_ts: str | None = None) -> bool:
        if msg.get("subtype") in SKIP_SUBTYPES or msg.get("bot_id") or not msg.get("user"):
            return False
        if msg.get("user") == self.bot_user_id:
            return False
        text = await self.api.clean_text(msg.get("text") or "")
        files = [f.get("name") or f.get("title") or "plik" for f in msg.get("files") or []]
        if files:
            text = (text + "\n" if text else "") + "\n".join(f"[plik: {f}]" for f in files)
        if not text.strip():
            return False
        user = await self.api.user(msg["user"])
        entity_id = None
        if user.get("email"):
            entity_id = await self.db.resolve_entity("gmail", user["email"])
        if entity_id is None:
            entity_id = await self.db.resolve_entity("slack", user["name"])
        ts = msg["ts"]
        event_id = await self.db.store_event(
            source="slack",
            source_id=f"{cid}:{ts}",
            event_type="message",
            timestamp=datetime.fromtimestamp(float(ts), tz=timezone.utc),
            title=f"#{cfg.get('name', cid)}",
            body=text,
            sender_entity_id=entity_id,
            category="daily" if cfg.get("daily") else "channel",
            content_hash=hashlib.sha1(text.encode()).hexdigest()[:16],
            metadata={
                "channel_id": cid,
                "channel_name": cfg.get("name"),
                "daily": bool(cfg.get("daily")),
                "user_id": msg["user"],
                "user_name": user["name"],
                "ts": ts,
                "thread_ts": thread_ts or msg.get("thread_ts"),
                "is_reply": bool(thread_ts),
                "reply_count": msg.get("reply_count", 0),
                "edited": bool(msg.get("edited")),
            },
        )
        return event_id is not None
