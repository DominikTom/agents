"""MCP Server for Claude.ai integration - exposes business data as tools."""

from __future__ import annotations

import json
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings

# Disable DNS rebinding protection - we're behind Caddy reverse proxy
# and handle authentication via Bearer token in BearerAuthMiddleware
_orig_ts_init = TransportSecurityMiddleware.__init__


def _init_no_dns_rebinding(self, settings=None):
    _orig_ts_init(self, TransportSecuritySettings(enable_dns_rebinding_protection=False))


TransportSecurityMiddleware.__init__ = _init_no_dns_rebinding

from src.mcp_server.formatters import (
    format_events,
    format_whatsapp_messages,
    format_metrics_summary,
    format_entity_list,
    format_ingestion_stats,
)

logger = logging.getLogger(__name__)

WARSAW = ZoneInfo("Europe/Warsaw")

mcp = FastMCP("agents-mcp", stateless_http=True)

# Database reference - set via set_mcp_db() during app startup
_db = None


def set_mcp_db(db):
    """Set the database instance for MCP tools to use."""
    global _db
    _db = db


def _get_db():
    if _db is None:
        raise RuntimeError("MCP database not initialized")
    return _db


@mcp.tool()
async def search(query: str, limit: int = 20) -> str:
    """Search across all business data - emails, WhatsApp messages, calendar events.

    Use this to find information about any topic, person, or project across all data sources.
    Examples: "Comfy reklamacja", "sesja zdjęciowa", "zamówienie 12345"
    """
    db = _get_db()
    events = await db.search_events(query, limit=min(limit, 50))
    if not events:
        return f"No results found for '{query}'."
    return format_events(events, title=f"Search results for '{query}'")


@mcp.tool()
async def get_sales(days: int = 7, store: str | None = None) -> str:
    """Get sales and revenue data per store per day.

    Returns order counts, revenue, average order value for each store.
    Stores: mybed.pl, mybed.de, mittohome.pl, etc.
    """
    db = _get_db()
    since = date.today() - timedelta(days=days)
    metrics = await db.get_metrics(store=store, since=since)
    if not metrics:
        return "No sales data available for the specified period."
    return format_metrics_summary(metrics, days=days)


@mcp.tool()
async def get_events(source: str, days: int = 1, limit: int = 20) -> str:
    """Get events from a specific data source.

    Available sources: gmail, calendar, whatsapp
    Use days parameter to control time range (default: last 1 day).
    """
    db = _get_db()
    since = datetime.now(WARSAW) - timedelta(days=days)
    events = await db.get_events(source=source, since=since, limit=min(limit, 50))
    if not events:
        return f"No events from {source} in the last {days} day(s)."
    return format_events(events, title=f"{source.upper()} events (last {days}d)")


@mcp.tool()
async def get_person_activity(person_name: str, days: int = 7) -> str:
    """Get cross-source activity for a specific person (emails, WhatsApp, calendar).

    Use the person's full name or a unique first name (e.g. "Karolina", "Maciej Żydziak").
    """
    db = _get_db()
    entity_id = await db.resolve_entity("people", person_name)
    if not entity_id:
        return f"Person '{person_name}' not found. Use list_people() to see team members."
    since = datetime.now(WARSAW) - timedelta(days=days)
    events = await db.get_events(sender_entity_id=entity_id, since=since, limit=50)
    if not events:
        return f"No recorded activity for {person_name} in the last {days} days."
    return format_events(events, title=f"Activity for {person_name} (last {days} days)")


@mcp.tool()
async def get_tasks_overview() -> str:
    """MyBed OS overview: my overdue/today tasks, team overdue per person, blockers, projects needing attention, pending decisions."""
    from src.connectors.os_mybed import OSClient

    client = OSClient()
    if not client.configured:
        return "MyBed OS is not configured in agents (OS_SUPABASE_SERVICE_KEY)."
    snap = await client.snapshot()
    return json.dumps({
        "my_tasks": snap.my_tasks(),
        "team": snap.team_load(),
        "blockers": snap.blockers(),
        "projects_attention": snap.projects_attention(),
        "decisions_pending": snap.decisions_pending(),
    }, ensure_ascii=False, indent=1, default=str)


@mcp.tool()
async def get_chat_summaries(days: int = 1, only_important: bool = False) -> str:
    """AI summaries of WhatsApp chats per day: summary, decisions, open questions, whether a reply is needed."""
    db = _get_db()
    since = date.today() - timedelta(days=max(0, days - 1))
    digests = await db.get_chat_digests(since, limit=100)
    if only_important:
        digests = [d for d in digests if d["importance"] == "high"]
    if not digests:
        return "No chat summaries for this period yet."
    parts = []
    for d in digests:
        line = f"[{d['day']}] {d['chat_name']} ({d['importance']}): {d['summary']}"
        if d["decisions"]:
            line += "\n  Decyzje: " + "; ".join(d["decisions"])
        if d["open_questions"]:
            line += "\n  Otwarte: " + "; ".join(d["open_questions"])
        if d["needs_reply"]:
            line += f"\n  Czeka na odpowiedź: {d['reply_hint']}"
        parts.append(line)
    return "\n\n".join(parts)


@mcp.tool()
async def get_awaiting_replies() -> str:
    """WhatsApp chats and e-mails where someone is waiting for Dominik's reply."""
    db = _get_db()
    wa = await db.get_whatsapp_awaiting(days=5, limit=30)
    mail = await db.get_email_awaiting(days=3, limit=30)
    parts = ["WhatsApp:"]
    parts += [f"  {w['chat_name']} — {w['pending_count']} msg since {w['waiting_since']:%d.%m %H:%M}: {(w['last_body'] or '')[:160]}" for w in wa] or ["  (nothing)"]
    parts.append("E-mail:")
    parts += [f"  {e['title']} — from {e['metadata'].get('sender_name') or e['metadata'].get('from')} ({e['timestamp']:%d.%m %H:%M})" for e in mail] or ["  (nothing)"]
    return "\n".join(parts)


@mcp.tool()
async def get_commitments(status: str = "open") -> str:
    """Commitments detected in chats: things Dominik promised (mine), was asked to do (ask), or others promised him (theirs)."""
    db = _get_db()
    rows = await db.list_commitments(status=status, limit=100)
    if not rows:
        return "No commitments."
    return "\n".join(
        f"[{c['direction']}] {c['title']} — {c.get('counterpart') or ''} "
        f"(termin: {c['due_date'] or c.get('due_hint') or '-'}; {c['source']}: {c.get('chat_name') or ''})"
        for c in rows
    )


@mcp.tool()
async def get_latest_report(report: str = "morning_briefing") -> str:
    """Latest generated report text. report: morning_briefing | daily_wrap | weekly_review."""
    db = _get_db()
    r = await db.get_latest_report(report)
    if not r:
        return "No report of this type yet."
    return f"{r.get('title') or report} ({r['generated_at']:%Y-%m-%d %H:%M})\n\n{r.get('summary') or ''}\n\n{r['body']}"


@mcp.tool()
async def get_team_updates(day: str = "") -> str:
    """Team daily reports: MyBed OS Daily Update (and, before the switch day set in the panel, Slack for people
    without an OS report). Per person: done / in progress / waiting / plan / needs, changes the author approved
    in OS, who did not report, escalations. day: YYYY-MM-DD, default = last workday (or today after 15:00)."""
    from src.common.timeutil import now, today
    from src.processing.team_updates import last_workday, team_overview

    db = _get_db()
    try:
        d = date.fromisoformat(day) if day else None
    except ValueError:
        return "Bad date, use YYYY-MM-DD."
    if d is None:
        t = today()
        d = t if t.weekday() < 5 and now().hour >= 15 else last_workday(t)
    o = await team_overview(db, d)
    if o["mode"] != "os" and not o["channels_configured"] and not o["updates"] and o["os_error"]:
        return f"No daily reports source: {o['os_error']}; no Slack daily-report channels configured."
    keys = ("summary", "done", "in_progress", "waiting", "next", "needs", "blockers", "outside_os", "plan_quality",
            "applied", "pending_suggestions", "focus", "attention", "os_updates", "not_in_os")
    return json.dumps({
        "day": d.isoformat(),
        "source": o["mode"],
        "switch_to_os_only_on": o["cutover"],
        "missing_report": [m["name"] for m in o["missing"]],
        "missing_check": o["missing_status"],
        "not_yet_reported": [m["name"] for m in o["not_yet"]],
        "absent": o["absent"],
        "escalations": [e["text"] for e in o["escalations"]],
        "posted_but_not_analyzed": o["not_analyzed"],
        "no_plan": o["no_plan"],
        "os_error": o["os_error"],
        "people": [{"person": u["person_name"], "from": u.get("source", "slack"), "link": u.get("link"),
                    **{k: v for k, v in (u["data"] or {}).items() if k in keys and v not in (None, [], "", 0)}}
                   for u in o["updates"]],
    }, ensure_ascii=False, indent=1, default=str)


@mcp.tool()
async def get_daily_summary(days: int = 3) -> str:
    """Get recent AI-generated daily summaries.

    These contain business activity summaries, key metrics, and open items.
    """
    db = _get_db()
    summaries = await db.get_recent_summaries("daily", limit=days)
    if not summaries:
        return "No daily summaries available yet."

    parts = ["Recent daily summaries:"]
    for s in summaries:
        parts.append(f"\n--- {s['period_start']} ---")
        parts.append(s["summary_text"][:500])
        if len(s["summary_text"]) > 500:
            parts.append("... (truncated)")

    return "\n".join(parts)


@mcp.tool()
async def get_whatsapp_messages(
    chat_name: str | None = None,
    person_name: str | None = None,
    limit: int = 20,
) -> str:
    """Get recent WhatsApp messages from business chats.

    Optionally filter by chat name (e.g. "MyBed Team") or person name (e.g. "Karolina").
    """
    db = _get_db()
    messages = await db.get_recent_whatsapp_messages(limit=1000 if (chat_name or person_name) else min(limit, 50))

    if chat_name:
        messages = [
            m
            for m in messages
            if chat_name.lower() in (m.get("chat_name") or "").lower()
        ]
    if person_name:
        messages = [
            m
            for m in messages
            if person_name.lower() in (m.get("sender_name") or "").lower()
        ]

    messages = messages[: min(limit, 50)]
    if not messages:
        filter_desc = ""
        if chat_name:
            filter_desc = f" in chat '{chat_name}'"
        elif person_name:
            filter_desc = f" from '{person_name}'"
        return f"No WhatsApp messages found{filter_desc}."

    return format_whatsapp_messages(messages)


@mcp.tool()
async def list_people() -> str:
    """List all team members registered in the system with their roles."""
    db = _get_db()
    entities = await db.get_all_entities("person")
    if not entities:
        return "No team members registered in the system."
    return format_entity_list(entities)


@mcp.tool()
async def get_topics(limit: int = 10) -> str:
    """Get active cross-source topics - business threads that span multiple data sources."""
    db = _get_db()
    topics = await db.get_active_topics(limit=min(limit, 20))
    if not topics:
        return "No active topics tracked yet."

    parts = ["Active topics:"]
    for t in topics:
        event_count = t.get("event_count", 0)
        parts.append(f"\n  {t['name']} ({event_count} events)")
        if t.get("description"):
            parts.append(f"    {t['description'][:200]}")
        if t.get("category"):
            parts.append(f"    Category: {t['category']}")

    return "\n".join(parts)


@mcp.tool()
async def get_system_status() -> str:
    """Get system status - data source sync status, event counts, last sync times."""
    db = _get_db()
    stats = await db.get_ingestion_stats()
    if not stats:
        return "No ingestion data available."
    return format_ingestion_stats(stats)


def create_mcp_app():
    """Create the MCP ASGI app for mounting on FastAPI."""
    return mcp.streamable_http_app()
