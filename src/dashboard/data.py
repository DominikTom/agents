"""Data assembly for panel pages (with short in-process caches for remote sources)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from src.common.config import get_env_optional
from src.common.timeutil import day_range, now, today
from src.connectors.dash import DashClient
from src.connectors.os_mybed import OSClient
from src.connectors.whatsapp_bridge import BridgeClient, BridgeError
from src.outputs.email_output import email_configured
from src.storage.database import Database, _j

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, object]] = {}


async def cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = await fn()
    _cache[key] = (time.monotonic(), value)
    return value


def invalidate(prefix: str = "") -> None:
    for k in [k for k in _cache if k.startswith(prefix)]:
        _cache.pop(k, None)


async def bridge_status() -> dict:
    async def load():
        try:
            return await asyncio.wait_for(BridgeClient(timeout=4).status(), timeout=5)
        except (BridgeError, asyncio.TimeoutError) as e:
            return {"status": "unreachable", "connected": False, "error": str(e) or "timeout"}
    return await cached("bridge", 10, load)


async def os_snapshot():
    client = OSClient()
    if not client.configured:
        return None

    async def load():
        try:
            return await client.snapshot()
        except Exception as e:
            logger.warning(f"OS snapshot failed: {e}")
            return None
    return await cached("os", 300, load)


async def dash_overview():
    client = DashClient()
    if not client.configured:
        return None

    async def load():
        try:
            return await client.overview()
        except Exception as e:
            logger.warning(f"Dash overview failed: {e}")
            return None
    return await cached("dash", 600, load)


async def erp_yesterday(db: Database) -> dict | None:
    """Fallback KPIs from IdeaERP metrics when dash isn't configured."""
    y = today() - timedelta(days=1)
    rows = await db.get_metrics(since=y, until=y)
    if not rows:
        return None
    orders = sum(float(r["metric_value"]) for r in rows if r["metric_name"] == "orders_count")
    revenue = sum(float(r["metric_value"]) for r in rows if r["metric_name"] == "revenue" and (r.get("currency") or "PLN") == "PLN")
    return {"orders": int(orders), "revenue": revenue}


def config_status() -> dict:
    return {
        "anthropic": bool(get_env_optional("ANTHROPIC_API_KEY")),
        "os": OSClient().configured,
        "dash": DashClient().configured,
        "email": email_configured(),
        "bridge_token": bool(get_env_optional("BRIDGE_TOKEN")),
        "google": bool(get_env_optional("GOOGLE_CREDENTIALS_PATH")),
        "slack": bool(get_env_optional("SLACK_BOT_TOKEN")),
        "ideaerp": bool(get_env_optional("IDEAERP_API_TOKEN")),
        "mcp_key": bool(get_env_optional("MCP_API_KEY")),
    }


SOURCES = [
    # key, label, icon, expected max age of newest ingest (hours) during working hours
    ("whatsapp", "WhatsApp", "message-circle", 3),
    ("gmail", "Gmail", "mail", 3),
    ("calendar", "Kalendarz Google", "calendar", 6),
]


async def sources_health(db: Database) -> list[dict]:
    stats = {s["source"]: s for s in await db.get_ingestion_stats()}
    runs = await db.get_last_run_any(["whatsapp_sync", "gmail_sync", "calendar_sync", "ideaerp_sync", "os_people_sync", "chat_digests"])
    out = []
    for key, label, icon, max_age in SOURCES:
        s = stats.get(key, {})
        last = s.get("last_event")
        run = runs.get({"whatsapp": "whatsapp_sync", "gmail": "gmail_sync", "calendar": "calendar_sync"}[key])
        if run and run["status"] == "error":
            state = "error"
        elif not last:
            state = "empty"
        elif key != "calendar" and (now() - last).total_seconds() > max_age * 3600 * 8:
            state = "stale"
        else:
            state = "ok"
        out.append({"key": key, "label": label, "icon": icon, "state": state, "last_event": last,
                    "last_24h": s.get("last_24h", 0), "total": s.get("total", 0), "run": run})
    return out


async def pulpit(db: Database) -> dict:
    t = today()
    start, end = day_range(t)
    bridge, os_snap, dash, wa_wait, mail_wait, cal, commitments, digests, briefing, wrap = await asyncio.gather(
        bridge_status(),
        os_snapshot(),
        dash_overview(),
        db.get_whatsapp_awaiting(days=5, limit=8),
        db.get_email_awaiting(days=3, limit=8),
        db.get_calendar_events(start, end),
        db.list_commitments(status="open", limit=40),
        db.get_chat_digests(t - timedelta(days=1), limit=60),
        db.get_latest_report("morning_briefing"),
        db.get_latest_report("daily_wrap"),
    )
    hints = {d["chat_jid"]: d["reply_hint"] for d in digests if d["needs_reply"] and d.get("reply_hint")}
    for w in wa_wait:
        w["hint"] = hints.get(w["jid"], "")
    important = [d for d in digests if d["importance"] == "high"][:6]
    mine = [c for c in commitments if c["direction"] in ("mine", "ask")]
    return {
        "bridge": bridge,
        "os": os_snap.summary_counts() if os_snap else None,
        "os_my": os_snap.my_tasks() if os_snap else None,
        "os_blockers": os_snap.blockers()[:4] if os_snap else [],
        "dash": dash,
        "erp": None if dash else await erp_yesterday(db),
        "wa_wait": wa_wait,
        "mail_wait": mail_wait,
        "calendar": [e for e in cal if e["metadata"].get("my_status") != "declined"],
        "commitments": mine[:6],
        "commitments_total": len(mine),
        "theirs_total": len(commitments) - len(mine),
        "important": important,
        "briefing": briefing,
        "wrap": wrap,
        "config": config_status(),
    }


def parse_meta(report: dict | None) -> dict:
    return _j((report or {}).get("meta"), {}) or {}
