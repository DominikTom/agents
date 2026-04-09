"""Formatters for MCP tool responses - convert DB results to readable text."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")


def _fmt_time(dt: datetime | None) -> str:
    """Format datetime in Warsaw timezone."""
    if not dt:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=WARSAW)
    return dt.astimezone(WARSAW).strftime("%Y-%m-%d %H:%M")


def _truncate(text: str | None, max_len: int = 200) -> str:
    """Truncate text to max_len, replacing newlines with spaces."""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def format_events(events: list[dict], title: str | None = None, max_items: int = 30) -> str:
    """Format a list of events into readable text."""
    parts = []
    if title:
        parts.append(f"{title} ({len(events)} results):")

    for e in events[:max_items]:
        source = e.get("source", "?")
        ts = _fmt_time(e.get("timestamp"))
        sender = e.get("sender_name") or ""
        event_title = e.get("title") or ""
        body = _truncate(e.get("body"), 200)

        line = f"[{ts}] {source.upper()}"
        if sender:
            line += f" | {sender}"
        if event_title:
            line += f" | {event_title}"
        if body:
            line += f"\n  {body}"
        parts.append(line)

    if len(events) > max_items:
        parts.append(f"\n... and {len(events) - max_items} more results")

    return "\n".join(parts)


def format_whatsapp_messages(messages: list[dict], max_items: int = 30) -> str:
    """Format WhatsApp messages (which have chat_name instead of title)."""
    parts = [f"WhatsApp messages ({len(messages)} results):"]

    for m in messages[:max_items]:
        ts = _fmt_time(m.get("timestamp"))
        sender = m.get("sender_name") or "?"
        chat = m.get("chat_name") or ""
        body = _truncate(m.get("body"), 200)
        parts.append(f"[{ts}] {chat} | {sender}: {body}")

    if len(messages) > max_items:
        parts.append(f"\n... and {len(messages) - max_items} more")

    return "\n".join(parts)


def format_metrics_summary(metrics: list[dict], days: int = 7) -> str:
    """Format business metrics into a sales summary."""
    by_store: dict[str, dict[str, dict]] = {}
    currencies: dict[str, str] = {}

    for m in metrics:
        store = m["store"]
        d = str(m["date"])
        if store not in by_store:
            by_store[store] = {}
        if d not in by_store[store]:
            by_store[store][d] = {}
        by_store[store][d][m["metric_name"]] = m["metric_value"]
        if m.get("currency"):
            currencies[store] = m["currency"]

    parts = [f"Sales data (last {days} days):"]

    for store in sorted(by_store):
        currency = currencies.get(store, "PLN")
        parts.append(f"\n{store}:")
        for d in sorted(by_store[store], reverse=True):
            data = by_store[store][d]
            orders = int(data.get("orders_count", 0))
            revenue = data.get("revenue", 0)
            avg = data.get("avg_order_value", 0)
            parts.append(
                f"  {d}: {orders} orders, {revenue:,.0f} {currency} (avg {avg:,.0f})"
            )

    return "\n".join(parts)


def format_entity_list(entities: list[dict]) -> str:
    """Format entity list with roles and aliases."""
    parts = ["Team members:"]
    for e in entities:
        meta = e.get("metadata", {})
        if isinstance(meta, str):
            meta = json.loads(meta)
        role = meta.get("role", "")
        line = f"  {e['display_name']}"
        if role:
            line += f" ({role})"
        parts.append(line)
    return "\n".join(parts)


def format_ingestion_stats(stats: list[dict]) -> str:
    """Format ingestion stats for system status."""
    parts = ["Data source status:"]
    for s in stats:
        source = s["source"]
        total = s["total"]
        last_24h = s["last_24h"]
        last_1h = s.get("last_1h", 0)
        last_event = _fmt_time(s.get("last_event"))
        if last_1h > 0:
            status = "OK"
        elif last_24h > 0:
            status = "STALE"
        else:
            status = "DOWN"
        parts.append(
            f"  [{status}] {source.upper()}: {total} total, "
            f"{last_24h} in 24h, last: {last_event}"
        )
    return "\n".join(parts)
