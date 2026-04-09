"""MCP Server for Claude.ai integration - exposes business data as tools."""

from __future__ import annotations

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
    """Search across all business data - emails, WhatsApp messages, Asana tasks, calendar events.

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

    Available sources: gmail, calendar, asana, whatsapp, slack, ideaerp
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
    """Get cross-source activity for a specific person.

    Shows their emails, WhatsApp messages, Asana tasks, calendar events.
    Use person's first name or full name (e.g. "Patryk", "Karolina").
    """
    db = _get_db()
    # Try to resolve entity - fuzzy match on display_name works for partial names
    entity_id = await db.resolve_entity("people", person_name)
    if not entity_id:
        return (
            f"Person '{person_name}' not found in the system. "
            f"Use list_people() to see available team members."
        )

    since = datetime.now(WARSAW) - timedelta(days=days)
    events = await db.get_events(sender_entity_id=entity_id, since=since, limit=50)

    # Get velocity data
    velocity = await db.get_velocity(person_name, weeks=4)

    parts = [f"Activity for {person_name} (last {days} days):"]

    if events:
        parts.append(format_events(events))
    else:
        parts.append("No recorded activity in this period.")

    if velocity:
        parts.append("\nTask velocity (last 4 weeks):")
        for v in velocity:
            parts.append(
                f"  Week {v['week_start']}: "
                f"{v['tasks_closed']} closed, {v['tasks_overdue']} overdue"
            )

    return "\n".join(parts)


@mcp.tool()
async def get_tasks_overview(days: int = 7) -> str:
    """Get Asana tasks overview - recent task activity across team members.

    Shows task updates, completions, and status changes.
    """
    db = _get_db()
    since = datetime.now(WARSAW) - timedelta(days=days)
    events = await db.get_events(source="asana", since=since, limit=50)
    if not events:
        return "No Asana task data available. Tasks may not have been synced yet."
    return format_events(events, title=f"Asana tasks (last {days} days)")


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
    messages = await db.get_recent_whatsapp_messages(limit=min(limit, 50))

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
