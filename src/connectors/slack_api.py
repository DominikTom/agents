"""Slack Web API for the agents bot (Daily Agent): identity, channels, history, users, posting.

slack_sdk's WebClient is synchronous — every call runs in a worker thread. Errors are turned into
Polish messages that say what to do (invite the bot, add a scope and reinstall the app).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re

from src.common.config import get_env_optional

logger = logging.getLogger(__name__)

# Scopes the bot needs (Slack app → OAuth & Permissions → Bot Token Scopes)
REQUIRED_SCOPES = {
    "chat:write": "wysyłanie raportów",
    "channels:read": "lista kanałów publicznych",
    "groups:read": "lista kanałów prywatnych, na których jest bot",
    "channels:history": "czytanie kanałów publicznych",
    "groups:history": "czytanie kanałów prywatnych (np. #dev-daily-updates)",
    "users:read": "imiona i nazwiska autorów",
}
OPTIONAL_SCOPES = {"users:read.email": "dopasowanie autorów do ludzi z OS po e-mailu"}

# Channels where the team posts daily reports (found in the workspace) — shown as "invite the bot here"
SUGGESTED_DAILY = ["dev-daily-updates", "kamila-daily-update", "luiza-daily-update", "kordian-daily-update",
                   "dorota-case", "office-update"]
DAILY_NAME = re.compile(r"daily|update|case|raport|standup", re.I)


class SlackError(RuntimeError):
    pass


def _explain(err: Exception) -> str:
    resp = getattr(err, "response", None)
    code = (resp.get("error") if resp is not None else None) or str(err)
    if code == "missing_scope":
        needed = resp.get("needed") if resp is not None else ""
        return (f"Brak uprawnienia Slack: {needed}. Dodaj je w api.slack.com → Twoja aplikacja → OAuth & Permissions "
                "→ Bot Token Scopes, potem „Reinstall to Workspace” i wklej nowy token do .env, jeśli się zmienił.")
    return {
        "not_in_channel": "Bota nie ma na tym kanale — wpisz na kanale /invite @nazwa-bota.",
        "channel_not_found": "Nie ma takiego kanału albo bot go nie widzi (kanał prywatny bez bota).",
        "invalid_auth": "Token Slacka jest nieprawidłowy (SLACK_BOT_TOKEN w .env).",
        "not_authed": "Brak tokenu Slacka (SLACK_BOT_TOKEN w .env).",
        "account_inactive": "Aplikacja Slack została odinstalowana albo token unieważniony.",
        "token_revoked": "Token Slacka został unieważniony — zainstaluj aplikację ponownie.",
        "ratelimited": "Slack ogranicza liczbę zapytań — spróbuję przy następnej synchronizacji.",
        "is_archived": "Kanał jest zarchiwizowany.",
    }.get(code, f"Slack: {code}")


class SlackAPI:
    def __init__(self, token: str | None = None):
        self.token = token if token is not None else (get_env_optional("SLACK_BOT_TOKEN") or "")
        self._client = None
        self._users: dict[str, dict] = {}
        self.last_unresolved: list[str] = []  # mention ids clean_text could not turn into names

    @property
    def configured(self) -> bool:
        return bool(self.token)

    @property
    def client(self):
        if self._client is None:
            from slack_sdk import WebClient

            self._client = WebClient(token=self.token, timeout=20)
        return self._client

    async def _call(self, method: str, **kwargs) -> dict:
        if not self.configured:
            raise SlackError("Brak tokenu Slacka (SLACK_BOT_TOKEN w .env).")
        try:
            resp = await asyncio.to_thread(getattr(self.client, method), **kwargs)
            return resp.data if hasattr(resp, "data") else dict(resp)
        except Exception as e:  # SlackApiError and network errors
            raise SlackError(_explain(e)) from e

    async def identity(self) -> dict:
        """Bot name, team and granted scopes (from the x-oauth-scopes header of auth.test)."""
        if not self.configured:
            raise SlackError("Brak tokenu Slacka (SLACK_BOT_TOKEN w .env).")
        try:
            resp = await asyncio.to_thread(self.client.auth_test)
        except Exception as e:
            raise SlackError(_explain(e)) from e
        headers = {k.lower(): v for k, v in (getattr(resp, "headers", {}) or {}).items()}
        raw = headers.get("x-oauth-scopes") or ""
        if isinstance(raw, list):
            raw = ",".join(raw)
        scopes = sorted({x.strip() for x in raw.split(",") if x.strip()})
        data = resp.data if hasattr(resp, "data") else dict(resp)
        return {"bot": data.get("user"), "bot_id": data.get("user_id"), "team": data.get("team"),
                "url": data.get("url"), "scopes": scopes}

    async def member_channels(self) -> list[dict]:
        """Channels (public + private) the bot is a member of."""
        out, cursor = [], None
        for _ in range(20):
            data = await self._call("users_conversations", types="public_channel,private_channel",
                                    exclude_archived=True, limit=200, cursor=cursor)
            for ch in data.get("channels", []):
                out.append({"id": ch["id"], "name": ch.get("name", ch["id"]), "private": ch.get("is_private", False)})
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return sorted(out, key=lambda c: c["name"])

    async def history(self, channel: str, oldest: str, max_messages: int = 1000) -> list[dict]:
        """Top-level messages newer than `oldest` (ts string), oldest first."""
        out, cursor = [], None
        while len(out) < max_messages:
            data = await self._call("conversations_history", channel=channel, oldest=oldest, limit=200,
                                    cursor=cursor, inclusive=False)
            out.extend(data.get("messages", []))
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not data.get("has_more") or not cursor:
                break
        return sorted(out, key=lambda m: float(m.get("ts", 0)))

    async def replies(self, channel: str, thread_ts: str) -> list[dict]:
        data = await self._call("conversations_replies", channel=channel, ts=thread_ts, limit=200)
        return [m for m in data.get("messages", []) if m.get("ts") != thread_ts]

    async def user(self, user_id: str) -> dict:
        if user_id not in self._users:
            try:
                data = await self._call("users_info", user=user_id)
                u = data.get("user") or {}
                prof = u.get("profile") or {}
                self._users[user_id] = {
                    "id": user_id,
                    "name": prof.get("real_name") or u.get("real_name") or prof.get("display_name") or u.get("name") or user_id,
                    "email": (prof.get("email") or "").lower(),
                    "bot": bool(u.get("is_bot")),
                }
            except SlackError as e:
                logger.warning(f"Slack users.info {user_id}: {e}")
                self._users[user_id] = {"id": user_id, "name": user_id, "email": "", "bot": False}
        return self._users[user_id]

    async def resolve_target(self, target: str) -> str:
        """'#name' → channel id (channels the bot is in); ids (C…/G…/U…) pass through."""
        target = target.strip()
        if not target.startswith("#"):
            return target
        name = target[1:].lower()
        try:
            for ch in await self.member_channels():
                if ch["name"].lower() == name:
                    return ch["id"]
        except SlackError as e:
            logger.info(f"Slack: channel list unavailable ({e}), posting to {target} by name")
        # not found among the bot's channels (or no groups:read) — chat.postMessage accepts names too
        # (public channels with chat:write.public); its errors are explained by _explain
        return target

    async def post(self, target: str, text: str) -> str:
        channel = await self.resolve_target(target)
        data = await self._call("chat_postMessage", channel=channel, text=text, unfurl_links=False, unfurl_media=False,
                                mrkdwn=True)
        return data.get("channel") or channel

    async def clean_text(self, text: str) -> str:
        """Slack markup → readable text: mentions, channel links, URLs, entities."""
        text = text or ""
        self.last_unresolved = []
        for uid in sorted(set(re.findall(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", text))):
            name = (await self.user(uid))["name"]
            if name == uid:
                self.last_unresolved.append(uid)  # users.info failed — the stored text keeps the raw id for now
            # function replacement: a name is data, never a regex template (backslashes, \g<…>)
            text = re.sub(rf"<@{uid}(?:\|[^>]*)?>", lambda _m, repl=f"@{name}": repl, text)
        text = re.sub(r"<#[CG][A-Z0-9]+\|([^>]*)>", r"#\1", text)
        text = re.sub(r"<!(here|channel|everyone)[^>]*>", r"@\1", text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
        text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
        text = re.sub(r"<mailto:([^|>]+)\|([^>]+)>", r"\2", text)
        return html.unescape(text)
