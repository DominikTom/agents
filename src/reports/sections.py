"""Data builders for report sections.

Each builder returns compact, JSON-serialisable data (only what the model
needs — tokens cost money and dilute attention) or raises; failures are
reported to the model as an unavailable source instead of breaking the run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from src.common.timeutil import day_range, day_start, hours_since, to_local, today, week_start
from src.connectors.dash import DashClient
from src.connectors.os_mybed import OSClient, OSSnapshot
from src.storage.database import Database, _j

logger = logging.getLogger(__name__)


@dataclass
class ReportContext:
    db: Database
    kind: str  # morning | wrap | weekly
    general: dict
    now: datetime
    window_start: datetime
    config: dict = field(default_factory=dict)
    _os: OSSnapshot | None = None
    _os_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _dash: dict | None = None
    _dash_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def today(self) -> date:
        return self.now.date()

    async def os(self) -> OSSnapshot:
        async with self._os_lock:
            if self._os is None:
                client = OSClient()
                if not client.configured:
                    raise RuntimeError("MyBed OS nie jest skonfigurowany (OS_SUPABASE_SERVICE_KEY)")
                self._os = await client.snapshot()
            return self._os

    async def dash(self) -> dict:
        async with self._dash_lock:
            if self._dash is None:
                client = DashClient()
                if not client.configured:
                    raise RuntimeError("Dash nie jest skonfigurowany (DASH_SUPABASE_SERVICE_KEY)")
                self._dash = await client.overview(self.today - timedelta(days=1))
            return self._dash


def report_window(kind: str, now: datetime) -> datetime:
    """Start of the period a report covers."""
    d = now.date()
    if kind == "weekly":
        return day_start(week_start(d))
    if kind == "wrap":
        return day_start(d)
    # morning: since 16:00 of the previous working day (Monday → Friday)
    back = 3 if d.weekday() == 0 else 1
    return day_start(d - timedelta(days=back)) + timedelta(hours=16)


_DAYS = ["pn", "wt", "śr", "cz", "pt", "sb", "nd"]


def _t(dt) -> str:
    dt = to_local(dt)
    return f"{_DAYS[dt.weekday()]} {dt.strftime('%d.%m %H:%M')}" if dt else ""


# ─── Builders ────────────────────────────────────────────────────────────────


async def build_sales(ctx: ReportContext) -> dict:
    out: dict = {}
    try:
        d = await ctx.dash()
        out["dzien"] = d["date"]
        out["razem"] = d["total"]
        out["sklepy"] = d["shops"]
        out["tydzien_do_dzis"] = d["week_to_date"]
        out["miesiac_do_dzis"] = d["month_to_date"]
        out["ostatnie_7_dni"] = d["last_7_days"]
        if ctx.kind == "weekly":
            out["dzienne_30d"] = [{k: s[k] for k in ("date", "revenue", "orders")} for s in d["series"][-14:]]
    except Exception as e:
        out["dash_blad"] = str(e)
    if ctx.kind in ("wrap", "weekly"):
        # Today so far from IdeaERP (hourly metrics)
        rows = await ctx.db.get_metrics(since=ctx.today, until=ctx.today)
        today_rows: dict[str, dict] = {}
        for r in rows:
            today_rows.setdefault(r["store"], {"currency": r.get("currency")})[r["metric_name"]] = float(r["metric_value"])
        if today_rows:
            out["dzis_do_tej_pory_ideaerp"] = today_rows
    if "dash_blad" in out and len(out) == 1:
        raise RuntimeError(out["dash_blad"])
    return out


async def build_marketing(ctx: ReportContext) -> dict:
    d = await ctx.dash()
    return {"dzien": d["date"], **d["marketing"]}


async def build_showrooms(ctx: ReportContext) -> list:
    d = await ctx.dash()
    return d["showrooms"]


async def build_calendar(ctx: ReportContext) -> dict:
    if ctx.kind == "morning":
        start, end = day_range(ctx.today)
        label = "dzis"
    elif ctx.kind == "wrap":
        nxt = ctx.today + timedelta(days=3 if ctx.today.weekday() == 4 else 1)
        start, end = day_range(nxt)
        label = "nastepny_dzien_roboczy"
    else:
        monday = week_start(ctx.today) + timedelta(days=7)
        start, end = day_start(monday), day_start(monday + timedelta(days=5))
        label = "przyszly_tydzien"
    events = await ctx.db.get_calendar_events(start, end)
    items = []
    for e in events:
        m = e["metadata"]
        if m.get("my_status") == "declined":
            continue
        items.append({
            "kiedy": "cały dzień" if m.get("all_day") else _t(e["timestamp"]),
            "tytul": e.get("title"),
            "uczestnicy": [a for a in (m.get("attendees") or [])][:8],
            "miejsce": m.get("location") or ("Google Meet" if m.get("hangout_link") else ""),
            "agenda": (m.get("description") or "")[:300],
        })
    return {label: items}


async def build_awaiting(ctx: ReportContext) -> dict:
    wa = await ctx.db.get_whatsapp_awaiting(days=5, limit=25)
    digests = await ctx.db.get_chat_digests(ctx.today - timedelta(days=2), jid=None, limit=200)
    hints = {}
    for d in digests:
        if d["needs_reply"] and d["chat_jid"] not in hints:
            hints[d["chat_jid"]] = d.get("reply_hint")
    emails = await ctx.db.get_email_awaiting(days=3, limit=20)
    return {
        "whatsapp": [
            {
                "czat": w["chat_name"],
                "grupa": w["is_group"],
                "od": w["last_sender"],
                "czeka_godzin": round(hours_since(w["waiting_since"]), 1),
                "wiadomosci": w["pending_count"],
                "ostatnia": (w["last_body"] or "")[:300],
                "czego_oczekuje": hints.get(w["jid"], ""),
            }
            for w in wa
        ],
        "email": [
            {
                "od": e.get("sender_display") or e["metadata"].get("sender_name") or e["metadata"].get("from"),
                "temat": e.get("title"),
                "czeka_godzin": round(hours_since(e["timestamp"]), 1),
                "wazny": e.get("priority") == "high",
                "fragment": (e.get("body") or "")[:400],
            }
            for e in emails
        ],
    }


async def build_commitments(ctx: ReportContext) -> dict:
    rows = await ctx.db.list_commitments(status="open", limit=80)

    def view(c):
        return {
            "co": c["title"],
            "z_kim": c.get("counterpart"),
            "termin": c["due_date"].isoformat() if c.get("due_date") else (c.get("due_hint") or ""),
            "zalegle": bool(c.get("due_date") and c["due_date"] < ctx.today),
            "zrodlo": f"{c['source']}: {c.get('chat_name') or ''}",
            "od": _t(c.get("source_at")),
        }

    return {
        "twoje": [view(c) for c in rows if c["direction"] in ("mine", "ask")],
        "czekasz_na": [view(c) for c in rows if c["direction"] == "theirs"],
    }


async def build_chats(ctx: ReportContext) -> list:
    since_day = ctx.window_start.date()
    digests = await ctx.db.get_chat_digests(since_day, limit=150)
    vip = {v.lower() for v in ctx.general.get("vip", [])}
    out = []
    for d in digests:
        if d["importance"] == "low" and ctx.kind != "wrap":
            continue
        if d.get("last_message_at") and d["last_message_at"] < ctx.window_start and ctx.kind == "morning":
            continue
        out.append({
            "czat": d["chat_name"],
            "dzien": d["day"].isoformat(),
            "waga": d["importance"],
            "vip": (d["chat_name"] or "").lower() in vip,
            "streszczenie": d["summary"],
            "decyzje": d["decisions"],
            "otwarte": d["open_questions"],
            "sprawy": d["topics"],
            "wymaga_odpowiedzi": d["reply_hint"] if d["needs_reply"] else "",
        })
    if ctx.kind == "weekly":
        out = [o for o in out if o["waga"] == "high"] + [o for o in out if o["waga"] == "normal"][:40]
    return out


async def build_email(ctx: ReportContext) -> list:
    events = await ctx.db.get_events(source="gmail", since=ctx.window_start, limit=150)
    out = []
    for e in events:
        m = _j(e.get("metadata"), {})
        labels = m.get("labels") or []
        if m.get("bulk") or any(l.startswith("CATEGORY_") and l != "CATEGORY_PERSONAL" for l in labels):
            continue
        out.append({
            "kiedy": _t(e["timestamp"]),
            "od": e.get("sender_name") or m.get("sender_name") or m.get("from"),
            "do_mnie_wyslany": not m.get("from_me"),
            "temat": e.get("title"),
            "tresc": (e.get("body") or "")[:600],
            "wazny": e.get("priority") == "high",
        })
    return out[:60]


async def build_os_me(ctx: ReportContext) -> dict:
    os_ = await ctx.os()
    return os_.my_tasks()


async def build_team(ctx: ReportContext) -> dict:
    os_ = await ctx.os()
    return {
        "obciazenie": os_.team_load()[:12],
        "blokery": os_.blockers(),
        "decyzje_do_podjecia": os_.decisions_pending(),
    }


async def build_projects(ctx: ReportContext) -> dict:
    os_ = await ctx.os()
    out = {"wymagaja_uwagi": os_.projects_attention()}
    if ctx.kind == "weekly":
        out["zamkniete_zadania_w_tygodniu"] = os_.completed_since(week_start(ctx.today))[:60]
        out["aktywne_projekty"] = os_.projects_active()
    return out


async def build_topics(ctx: ReportContext) -> list:
    topics = await ctx.db.get_active_topics(limit=15)
    out = []
    for t in topics:
        if t.get("last_activity_at") and t["last_activity_at"] < ctx.window_start - timedelta(days=2):
            continue
        full = await ctx.db.get_topic_with_events(t["id"], limit=8)
        out.append({
            "sprawa": t["name"],
            "opis": t.get("description"),
            "kategoria": t.get("category"),
            "zdarzenia": [
                {"zrodlo": e["source"], "kiedy": _t(e["timestamp"]), "tytul": e.get("title"),
                 "tresc": (e.get("body") or "")[:200]}
                for e in (full or {}).get("events", [])
            ],
        })
    return out


async def build_slack(ctx: ReportContext) -> list:
    from src.connectors import create_connector

    conn = create_connector("slack", ctx.config)
    if conn is None:
        raise RuntimeError("Slack nie jest skonfigurowany")
    result = await conn.fetch(since=ctx.window_start)
    if not result.ok:
        raise RuntimeError(result.error)
    return [{k: i.get(k) for k in ("channel", "user", "text")} for i in result.items[:80]]


BUILDERS = {
    "sales": build_sales,
    "marketing": build_marketing,
    "showrooms": build_showrooms,
    "calendar": build_calendar,
    "awaiting": build_awaiting,
    "commitments": build_commitments,
    "chats": build_chats,
    "email": build_email,
    "os_me": build_os_me,
    "team": build_team,
    "projects": build_projects,
    "topics": build_topics,
    "slack": build_slack,
}


async def gather_sections(ctx: ReportContext, keys: list[str]) -> tuple[dict, dict]:
    """Run builders for the enabled sections concurrently. Returns (data, errors)."""

    async def run(key):
        try:
            return key, await BUILDERS[key](ctx), None
        except Exception as e:
            logger.warning(f"Section {key} failed: {e}")
            return key, None, str(e)

    results = await asyncio.gather(*(run(k) for k in keys if k in BUILDERS))
    data = {k: v for k, v, err in results if err is None}
    errors = {k: err for k, v, err in results if err is not None}
    return data, errors


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
