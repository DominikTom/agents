"""The live day view ("Dziś") — one list of what Dominik has on his plate today, from every source.

Built on demand (no AI here — the AI work happens in the background jobs that read chats and e-mail):
- calendar of the day (mini timeline),
- open items from WhatsApp, e-mail and Dominik's own additions, typed: decyzja / odpowiedź / do zrobienia / czekasz na,
- conversations and e-mails waiting for his reply (live SQL — disappears as soon as he answers on any channel),
- his MyBed OS tasks overdue or due today,
- what got settled today, and source warnings (e.g. WhatsApp disconnected).
Used by the panel page /dzis and by the MCP tool `dzis`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from src.common.timeutil import day_range, hours_since, now, to_local, today

logger = logging.getLogger(__name__)

KINDS = {
    "decyzja": {"label": "Decyzja", "order": 0},
    "odpowiedz": {"label": "Odpowiedz", "order": 1},
    "zadanie": {"label": "Do zrobienia", "order": 2},
    "os": {"label": "Zadanie OS", "order": 3},
    "czekam": {"label": "Czekasz na", "order": 4},
}
SOURCE_LABELS = {"whatsapp": "WhatsApp", "gmail": "Mail", "manual": "Dodane", "os": "MyBed OS"}


def gmail_link(thread_id: str | None) -> str | None:
    return f"https://mail.google.com/mail/#all/{thread_id}" if thread_id else None


def item_link(c: dict) -> str | None:
    jid = c.get("chat_jid") or ""
    if jid.startswith("gmail:"):
        return gmail_link(jid[len("gmail:"):])
    if jid:
        return f"/chats/{jid}"
    return None


def _kind(c: dict) -> str:
    if c.get("direction") == "theirs":
        return "czekam"
    k = c.get("kind") or "zadanie"
    return k if k in KINDS else "zadanie"


def item_row(c: dict, ref: date) -> dict:
    due = c.get("due_date")
    kind = _kind(c)
    return {
        "id": c["id"],
        "type": "item",
        "kind": kind,
        "kind_label": KINDS[kind]["label"],
        "title": c["title"],
        "who": c.get("counterpart") or "",
        "source": c.get("source"),
        "source_label": SOURCE_LABELS.get(c.get("source"), c.get("source")),
        "where": c.get("chat_name") or "",
        "link": item_link(c),
        "due": due.isoformat() if isinstance(due, date) else None,
        "due_hint": c.get("due_hint") or "",
        "overdue": bool(due and due < ref),
        "due_today": bool(due and due == ref),
        "since": c.get("source_at"),
        "new_today": bool(c.get("source_at") and to_local(c["source_at"]).date() == ref),
        "quote": c.get("context") or "",
        "note": c.get("note") or "",
    }


def sort_key(r: dict):
    return (not r.get("overdue"), not r.get("due_today"), KINDS.get(r["kind"], {}).get("order", 9),
            -(to_local(r["since"]).timestamp() if isinstance(r.get("since"), datetime) else 0))


CAL_MODES = {"dzis": "Dziś", "jutro": "Jutro", "tydzien": "Tydzień"}
_WD = ["pn", "wt", "śr", "cz", "pt", "sb", "nd"]
# a day plan is what matters today — older items without a deadline wait in "pozostałe"
FOCUS_OVERDUE_DAYS = 14


def _event(e: dict) -> dict | None:
    m = e.get("metadata") or {}
    if m.get("my_status") == "declined":
        return None
    st = to_local(e["timestamp"])
    end_ts = m.get("end")
    try:
        en = to_local(datetime.fromisoformat(end_ts)) if isinstance(end_ts, str) and "T" in end_ts else None
    except ValueError:
        en = None
    return {
        "title": e.get("title") or "(bez tytułu)",
        "day": st.date() if st else None,
        "all_day": bool(m.get("all_day")),
        "start": st.strftime("%H:%M") if st else "",
        "end": en.strftime("%H:%M") if en else "",
        "start_min": st.hour * 60 + st.minute if st else None,
        "end_min": (en.hour * 60 + en.minute) if en else ((st.hour * 60 + st.minute + 30) if st else None),
        "where": m.get("location") or ("Google Meet" if m.get("hangout_link") else ""),
        "link": m.get("hangout_link") or m.get("html_link") or "",
        "people": [a for a in (m.get("attendees") or [])][:6],
    }


def day_label(d: date, ref: date) -> str:
    if d == ref:
        return "Dziś"
    if d == ref + timedelta(days=1):
        return "Jutro"
    return f"{_WD[d.weekday()]} {d.strftime('%d.%m')}"


async def calendar_days(db, first: date, n: int, ref: date | None = None) -> list[dict]:
    """Events of n days from `first`, one entry per day (empty days included)."""
    ref = ref or first
    rows = await db.get_calendar_events(day_range(first)[0], day_range(first + timedelta(days=n - 1))[1])
    days: dict[date, list] = {first + timedelta(days=i): [] for i in range(n)}
    for e in rows:
        ev = _event(e)
        if ev and ev["day"] in days:
            days[ev["day"]].append(ev)
    return [{"day": d, "label": day_label(d, ref), "weekend": d.weekday() >= 5,
             "events": sorted(evs, key=lambda x: (not x["all_day"], x["start_min"] or 0))}
            for d, evs in days.items()]


def in_focus(r: dict, ref: date) -> bool:
    """On today's plan: decisions, deadlines today / recently passed, and what came in since yesterday."""
    overdue_recent = r.get("overdue") and r.get("due") and date.fromisoformat(r["due"]) >= ref - timedelta(days=FOCUS_OVERDUE_DAYS)
    since = r.get("since")
    fresh = isinstance(since, datetime) and to_local(since).date() >= ref - timedelta(days=1)
    if r["kind"] == "czekam":
        return bool(overdue_recent or r.get("due_today") or r.get("new_today"))
    return bool(r["kind"] == "decyzja" or overdue_recent or r.get("due_today") or fresh)


async def build_day(db, ref: date | None = None, os_snapshot=None, cal: str = "dzis") -> dict:
    from src.reports.health import data_health

    ref = ref or today()
    start, end = day_range(ref)
    cal = cal if cal in CAL_MODES else "dzis"
    today_cal = await calendar_days(db, ref, 1)
    events = today_cal[0]["events"]
    if cal == "dzis":
        cal_days = today_cal
    elif cal == "jutro":
        cal_days = await calendar_days(db, ref + timedelta(days=1), 1, ref)
    else:
        cal_days = await calendar_days(db, ref, 7)

    open_items = await db.list_commitments(status="open", limit=200, include_snoozed=False)
    all_rows = [item_row(c, ref) for c in open_items]
    rows = [r for r in all_rows if in_focus(r, ref)]
    rest = sorted([r for r in all_rows if not in_focus(r, ref)], key=sort_key)

    wa = await db.get_whatsapp_awaiting(days=5, limit=40, hide_answered=False)
    mail = await db.get_email_awaiting(days=4, limit=40, hide_answered=False)
    digests = await db.get_chat_digests(ref - timedelta(days=2), limit=300)
    wa_hint = {}
    for d in digests:
        if d["needs_reply"] and d["chat_jid"] not in wa_hint:
            wa_hint[d["chat_jid"]] = d.get("reply_hint") or ""
    threads = await db.get_email_thread_hints([m["metadata"].get("thread_id") for m in mail if m["metadata"].get("thread_id")])
    replies, answered = [], []
    for w in wa:
        r = {
            "type": "reply", "kind": "odpowiedz", "kind_label": KINDS["odpowiedz"]["label"],
            "channel": "whatsapp", "ref": w["jid"], "title": wa_hint.get(w["jid"]) or (w.get("last_body") or "")[:160],
            "who": w.get("chat_name") or w.get("last_sender") or "", "source_label": "WhatsApp",
            "where": "grupa" if w.get("is_group") else "czat", "link": f"/chats/{w['jid']}",
            "waiting_h": round(hours_since(w["waiting_since"])), "since": w["waiting_since"],
            "count": w.get("pending_count"), "answered_elsewhere": w.get("answered_elsewhere"),
        }
        (answered if r["answered_elsewhere"] else replies).append(r)
    for m in mail:
        meta = m["metadata"]
        t = threads.get(meta.get("thread_id")) or {}
        if t and not t.get("needs_reply"):
            continue  # the AI read of the thread says nobody waits (e.g. "dziękuję, otrzymałem")
        r = {
            "type": "reply", "kind": "odpowiedz", "kind_label": KINDS["odpowiedz"]["label"],
            "channel": "gmail", "ref": meta.get("thread_id"), "title": t.get("reply_hint") or m.get("title") or "",
            "who": m.get("sender_display") or meta.get("sender_name") or meta.get("from") or "",
            "source_label": "Mail", "where": m.get("title") or "", "link": gmail_link(meta.get("thread_id")),
            "waiting_h": round(hours_since(m["timestamp"])), "since": m["timestamp"],
            "important": m.get("priority") == "high" or t.get("importance") == "high",
            "answered_elsewhere": m.get("answered_elsewhere"),
        }
        (answered if r["answered_elsewhere"] else replies).append(r)
    replies.sort(key=lambda r: (not r.get("important"), -r["waiting_h"]))

    os_rows = []
    try:
        if os_snapshot is None:
            from src.connectors.os_mybed import OSClient

            client = OSClient()
            os_snapshot = await client.snapshot() if client.configured else None
        if os_snapshot is not None:
            mine = os_snapshot.my_tasks()
            for t in mine.get("overdue", []) + mine.get("today", []):
                due = t.get("due")
                os_rows.append({
                    "type": "os", "kind": "os", "kind_label": KINDS["os"]["label"], "title": t.get("title"),
                    "who": t.get("project") or "", "source_label": "MyBed OS", "link": t.get("link"),
                    "due": due, "overdue": bool(due and due < ref.isoformat()), "due_today": due == ref.isoformat(),
                    "status": t.get("status"),
                })
    except Exception as e:
        logger.warning(f"Day view: OS unavailable: {e}")

    sales = None
    try:
        from src.connectors.panel_erp import PanelERPClient

        panel = PanelERPClient()
        if panel.configured:
            sales = await panel.today_so_far()
    except Exception as e:
        logger.warning(f"Day view: today's sales unavailable: {e}")

    plan = sorted(rows, key=sort_key)
    closed = await db.resolved_since(start, limit=30)
    try:
        health = await data_health(db)
    except Exception as e:
        logger.warning(f"Day view: health check failed: {e}")
        health = {"warnings": []}
    counts = {k: sum(1 for r in plan if r["kind"] == k) for k in KINDS}
    counts["odpowiedz"] += len(replies)
    counts["os"] = len(os_rows)
    return {
        "day": ref,
        "now": now(),
        "events": events,
        "calendar": {"mode": cal, "days": cal_days, "modes": CAL_MODES},
        "plan": plan,
        "rest": rest,
        "replies": replies,
        "answered_elsewhere": answered,
        "os": os_rows,
        "closed": closed,
        "warnings": health.get("warnings", []),
        "replies_hidden": any(w.startswith(("WhatsApp", "Gmail")) for w in health.get("warnings", [])),
        "counts": counts,
        "sales": sales,
    }


def day_text(d: dict) -> str:
    """Compact text version for MCP / chat."""
    out = [f"Dziś {d['day'].strftime('%d.%m.%Y')} (stan na {to_local(d['now']).strftime('%H:%M')})"]
    if d["warnings"]:
        out.append("UWAGA: " + "; ".join(d["warnings"]))
    if d.get("sales"):
        t = d["sales"]["razem"]
        per = ", ".join(f"{k} {v['zamowienia']} zam./{v['wartosc_zl']:,.0f} zł".replace(",", " ")
                        for k, v in d["sales"]["sklepy"].items())
        out.append(f"Sprzedaż dziś do tej pory: {t['zamowienia']} zam., {t['wartosc_zl']:,.0f} zł".replace(",", " ")
                   + (f" ({per})" if per else ""))
    out.append("\nKalendarz:")
    out += [f"  {'cały dzień' if e['all_day'] else e['start'] + (('–' + e['end']) if e['end'] else '')} {e['title']}"
            + (f" ({e['where']})" if e["where"] else "") for e in d["events"]] or ["  (nic)"]
    for kind in ("decyzja", "odpowiedz", "zadanie", "czekam"):
        rows = [r for r in d["plan"] if r["kind"] == kind]
        if kind == "odpowiedz":
            rows = rows + d["replies"]
        if not rows:
            continue
        out.append(f"\n{KINDS[kind]['label']}:")
        for r in rows:
            tag = f"#{r['id']} " if r.get("type") == "item" else (f"[{r['channel']}:{r['ref']}] " if r.get("type") == "reply" else "")
            when = " · PO TERMINIE" if r.get("overdue") else (" · na dziś" if r.get("due_today") else "")
            wait = f" · czeka {r['waiting_h']} h" if r.get("waiting_h") is not None else ""
            out.append(f"  {tag}{r['title']} — {r.get('who') or '?'} ({r.get('source_label')}){when}{wait}")
    if d["os"]:
        out.append("\nZadania OS (zaległe i na dziś):")
        out += [f"  {t['title']}{' · PO TERMINIE' if t['overdue'] else ''} {t.get('link') or ''}" for t in d["os"]]
    if d.get("rest"):
        out.append(f"\nPozostałe otwarte sprawy (starsze, bez terminu na dziś): {len(d['rest'])} — list_items")
    if d["answered_elsewhere"]:
        out.append("\nOdpisałeś innym kanałem (sprawdź, czy temat zamknięty):")
        out += [f"  [{r['channel']}:{r['ref']}] {r['who']}: {r['title'][:100]}" for r in d["answered_elsewhere"]]
    if d["closed"]:
        out.append("\nZałatwione dziś:")
        out += [f"  #{c['id']} {c['title']} — {c.get('resolution') or 'zamknięte'}" for c in d["closed"]]
    return "\n".join(out)
