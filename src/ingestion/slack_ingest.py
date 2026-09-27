"""Slack → events. Reads the channels the bot is in and that are switched on in Źródła → Slack.

Channel settings live in the `settings` table (key "slack"):
    {"channels": {"C123": {"name": "dev-daily-updates", "read": true, "daily": true}}}
New channels get defaults from their name: daily-report channels (daily/update/case…) are read
and treated as team daily reports; everything else is off until switched on in the panel.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import time
from datetime import datetime, timezone

from src.connectors.slack_api import DAILY_NAME, SlackAPI, SlackError
from src.storage.database import Database

logger = logging.getLogger(__name__)

FIRST_RUN_DAYS = 7          # history loaded the first time a channel is read
RECHECK_DAYS = 4            # edits and new thread replies re-checked on every run (covers a weekend)
REPLY_EDIT_WINDOW = 6 * 3600  # threads active in the last hours are re-read, so edited replies are picked up
# Only real user messages; everything else (joins, tombstones of deleted posts, bots…) is skipped
KEEP_SUBTYPES = {None, "thread_broadcast", "file_share", "me_message"}


async def load_channel_settings(db: Database) -> dict:
    return (await db.get_setting("slack", {}) or {}).get("channels", {}) or {}


async def refresh_channels(db: Database, api: SlackAPI) -> dict:
    """Merge the bot's current channels into the stored settings (defaults for new ones)."""
    stored = await db.get_setting("slack", {}) or {}
    channels = copy.deepcopy(stored.get("channels") or {})  # deep: the comparison below must see changes
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
        seen_replies = await self.db.get_slack_thread_last_reply(cid)
        for msg in await self.api.history(cid, oldest):
            new += await self._safe_store(cid, cfg, msg)
            latest_reply = float(msg.get("latest_reply") or 0)
            # fetch a thread when it has replies we have not stored yet, or recent activity (edited replies)
            if msg.get("reply_count") and (latest_reply > seen_replies.get(msg["ts"], 0.0)
                                           or latest_reply > now - REPLY_EDIT_WINDOW):
                for reply in await self.api.replies(cid, msg["ts"]):
                    new += await self._safe_store(cid, cfg, reply, thread_ts=msg["ts"])
        return new

    async def _safe_store(self, cid: str, cfg: dict, msg: dict, thread_ts: str | None = None) -> int:
        """One malformed message must not stop the channel (or the channels after it)."""
        try:
            return 1 if await self._store(cid, cfg, msg, thread_ts) == "inserted" else 0
        except SlackError:
            raise
        except Exception as e:
            logger.warning(f"Slack #{cfg.get('name')}: skipped message {msg.get('ts')}: {e}")
            return 0

    async def _resolve_author(self, user: dict) -> int | None:
        """Immutable identifiers only: the e-mail (users:read.email; aliases synced from OS) or a Slack user id
        the CEO linked in Ludzie. Display names can be changed by anyone, so a name match is only suggested
        in Ludzie — never applied automatically."""
        if user.get("email"):
            eid = await self.db.resolve_alias("gmail", user["email"])
            if eid:
                return eid
        return await self.db.resolve_alias("slack", user["id"])

    async def _store(self, cid: str, cfg: dict, msg: dict, thread_ts: str | None = None) -> str | None:
        if msg.get("subtype") not in KEEP_SUBTYPES or msg.get("hidden") or msg.get("bot_id") or not msg.get("user"):
            return None
        if msg.get("user") in (self.bot_user_id, "USLACKBOT"):
            return None
        text = await self.api.clean_text(msg.get("text") or "")
        files = [f.get("name") or f.get("title") or "plik" for f in msg.get("files") or []]
        if files:
            text = (text + "\n" if text else "") + "\n".join(f"[plik: {f}]" for f in files)
        if not text.strip():
            return None
        user = await self.api.user(msg["user"])
        entity_id = await self._resolve_author(user)
        ts = msg["ts"]
        return await self.db.upsert_slack_event(
            source_id=f"{cid}:{ts}",
            timestamp=datetime.fromtimestamp(float(ts), tz=timezone.utc),
            title=f"#{cfg.get('name', cid)}",
            body=text,
            sender_entity_id=entity_id,
            category="daily" if cfg.get("daily") else "channel",
            # hash of Slack's own text + edit stamp: a transient users.info failure (mention rendered as an id)
            # is not an edit and must not rewrite the stored message
            content_hash=hashlib.sha1(f"{msg.get('text') or ''}|{(msg.get('edited') or {}).get('ts', '')}|"
                                      f"{len(msg.get('files') or [])}".encode()).hexdigest()[:16],
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
