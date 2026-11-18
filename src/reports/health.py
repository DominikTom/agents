"""Are the inputs fresh? — one answer for reports, the day view, the panel and MCP.

A report written on stale data must say so in its first line (e.g. WhatsApp disconnected for days means
Dominik's replies there are invisible, so items look open although he handled them).
"""

from __future__ import annotations

from datetime import datetime

from src.common.timeutil import fmt_short, now, to_local

# source -> (label, hours of silence that count as "not syncing" on a working day / any day)
LIMITS = {
    "whatsapp": ("WhatsApp", 4, 24),
    "gmail": ("Gmail", 6, 30),
}


def _silence_warning(label: str, last: datetime | None, work_h: int, any_h: int, current: datetime) -> str | None:
    if last is None:
        return f"{label}: brak jakichkolwiek danych"
    hours = (current - last).total_seconds() / 3600
    working = current.weekday() < 5 and 9 <= current.hour <= 20
    if hours > any_h or (working and hours > work_h):
        days = hours / 24
        span = f"{days:.0f} dni" if days >= 2 else f"{hours:.0f} h"
        return f"{label} nie dostarcza wiadomości od {fmt_short(last)} ({span})"
    return None


async def data_health(db, current: datetime | None = None) -> dict:
    """{"sources": {key: {"label", "last", "ok"}}, "warnings": [text], "whatsapp_down_since": iso | None}"""
    current = current or now()
    stats = {s["source"]: s for s in await db.get_ingestion_stats()}
    out: dict = {"sources": {}, "warnings": []}
    for key, (label, work_h, any_h) in LIMITS.items():
        last = (stats.get(key) or {}).get("last_event")
        warn = _silence_warning(label, last, work_h, any_h, current)
        out["sources"][key] = {"label": label, "last": to_local(last).isoformat(timespec="minutes") if last else None,
                               "ok": warn is None}
        if warn:
            out["warnings"].append(warn)
    try:
        cal = await db.last_ingest_at("calendar")
    except Exception:
        cal = None
    if cal is None or (current - cal).total_seconds() > 12 * 3600:
        out["warnings"].append("Kalendarz Google nie synchronizuje się" + (f" od {fmt_short(cal)}" if cal else ""))
    out["sources"]["calendar"] = {"label": "Kalendarz", "last": to_local(cal).isoformat(timespec="minutes") if cal else None,
                                  "ok": cal is not None and (current - cal).total_seconds() <= 12 * 3600}
    try:
        wd = await db.get_setting("wa_watchdog", {}) or {}
    except Exception:
        wd = {}
    if wd.get("down_since"):
        since = datetime.fromtimestamp(wd["down_since"], tz=current.tzinfo)
        out["whatsapp_down_since"] = since.isoformat(timespec="minutes")
        if out["sources"]["whatsapp"]["ok"]:
            out["warnings"].append(f"WhatsApp rozłączony od {fmt_short(since)} (bridge)")
    return out


def health_instruction(health: dict) -> str:
    """Prompt rule for the report writer when inputs are stale ("" when all is fine)."""
    if not health.get("warnings"):
        return ""
    lines = "; ".join(health["warnings"])
    return ("UWAGA — dane niepełne: " + lines + ". Zacznij raport (pierwsze zdanie podsumowania) od krótkiego "
            "ostrzeżenia o tym i o skutku: odpowiedzi Dominika z tego kanału nie są widoczne, więc sprawy z niego mogą "
            "wyglądać na otwarte, choć są załatwione. Nie pisz „brak nowych rozmów”, gdy źródło po prostu nie działa.")
