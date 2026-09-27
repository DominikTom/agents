"""Team daily reports written in MyBed OS (Daily Update, `os_entities` collection `dailyUpdates`).

Contract: MyBed OS repo, docs/DAILY_REPORTS_FOR_AGENTS.md. One row per person and day
(id `du-<YYYY-MM-DD>-<personId>`, `data.date` = the Warsaw calendar day the report is about).
The AI analysis already ran in OS and its result lives in the same row — it is read, never repeated.
Read-only: the agents never write to these rows.

Privacy (doc §6.2): no absence reason, no manager replies, no writing-time metrics, no quotes,
and rejected/expired suggestions are never presented as neglect.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from src.common.timeutil import WARSAW, is_workday, next_workday, now, previous_workday
from src.storage.database import Database

logger = logging.getLogger(__name__)

MISSING_CUTOFF = time(10, 0)   # a day counts as "no report" from 10:00 of the next workday (late reports allowed)
HISTORY_DAYS = 21              # look-back for escalations (streaks, repeated requests)
NEED_DAYS = 2                  # a request repeated for this many workdays is escalated
WAITING_REPORTS = 3            # the same task "waiting" in this many consecutive reports is escalated
MISSING_STREAK = 2             # workdays in a row without a report
BLOCKER_DAYS = 2

STATE_KEYS = {"done": "done", "progress": "in_progress", "waiting": "waiting", "none": "not_touched"}


@dataclass
class DailyData:
    rows: list[dict] = field(default_factory=list)      # report data dicts (all statuses)
    settings: dict | None = None
    people: dict[str, dict] = field(default_factory=dict)
    tasks: dict[str, dict] = field(default_factory=dict)
    blockers: list[dict] = field(default_factory=list)
    app_url: str = "https://os.mybed.cloud"
    error: str | None = None
    start: date | None = None
    end: date | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def name(self, pid: str | None) -> str:
        return (self.people.get(pid or "") or {}).get("name") or (pid or "")

    def for_day(self, day: date) -> list[dict]:
        key = day.isoformat()
        return [r for r in self.rows if r.get("date") == key]

    def participants(self) -> list[str]:
        """Who must report (dailySettings.participantIds). Reminders off (enabled=false) = nobody."""
        s = self.settings or {}
        if s.get("enabled") is False:
            return []
        return [p for p in s.get("participantIds") or [] if isinstance(p, str)]

    def reported(self, day: date) -> dict[str, str]:
        """personId → 'submitted' | 'off' for a day (drafts are not reports)."""
        return {r["authorId"]: r["status"] for r in self.for_day(day)
                if r.get("authorId") and r.get("status") in ("submitted", "off")}


def _date_range_filter(start: date, end: date) -> str:
    # ids sort by day: every row of days start..end lies between du-<start> and du-<end+1>
    return f'(id.gte."du-{start.isoformat()}",id.lt."du-{(end + timedelta(days=1)).isoformat()}")'


async def load_daily(start: date, end: date, client=None, snapshot=None) -> DailyData:
    """Reports of days start..end plus what is needed to read them (people, current task titles, blockers).
    An OS snapshot already loaded for the report is reused. Never raises — errors land in `.error`."""
    from src.connectors.os_mybed import OSClient

    client = client or OSClient()
    out = DailyData(app_url=client.app_url, start=start, end=end)
    if not client.configured:
        out.error = "MyBed OS nie jest skonfigurowany (OS_SUPABASE_SERVICE_KEY)"
        return out
    try:
        reports = client.rest.select("os_entities", {
            "select": "id,data", "collection": "eq.dailyUpdates", "and": _date_range_filter(start, end), "order": "id",
        })
        settings = client.rest.select("os_entities", {
            "select": "data", "collection": "eq.singleton", "id": "eq.dailySettings",
        })
        if snapshot is not None:
            rows, srows = await asyncio.gather(reports, settings)
            people, tasks, blockers = (snapshot.data.get(k, []) for k in ("people", "tasks", "blockers"))
        else:
            rows, srows, people, tasks, blockers = await asyncio.gather(
                reports, settings, client.collection("people"), client.collection("tasks"),
                client.collection("blockers"))
    except Exception as e:
        logger.warning(f"MyBed OS daily reports: {e}")
        out.error = f"MyBed OS niedostępny: {e}"
        return out
    out.rows = [r["data"] for r in rows if isinstance(r.get("data"), dict) and r["data"].get("date")]
    out.settings = (srows[0].get("data") if srows else None) or None
    out.people = {p["id"]: p for p in people if p.get("id")}
    out.tasks = {t["id"]: t for t in tasks if t.get("id")}
    out.blockers = list(blockers)
    return out


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _title(dd: DailyData, ref: dict) -> str:
    """Current task title (the report keeps a copy from the moment it was written)."""
    task = dd.tasks.get(ref.get("taskId") or "")
    return (task or {}).get("title") or ref.get("title") or ""


def _need_text(dd: DailyData, n: dict) -> str:
    who = dd.name(n.get("personId")) if n.get("personId") else (n.get("who") or "")
    text = (n.get("text") or "").strip()
    return f"{who}: {text}" if who and text else who or text


def _applied(dd: DailyData, s: dict) -> str | None:
    action, title = s.get("action"), s.get("targetTitle") or _title(dd, s)
    if action == "set_status":
        return f"{title}: {(s.get('from') or {}).get('status') or '?'} → {(s.get('to') or {}).get('status') or '?'}"
    if action == "set_due":
        return f"{title}: termin {(s.get('from') or {}).get('dueDate') or '—'} → {(s.get('to') or {}).get('dueDate') or '—'}"
    if action == "create_task":
        return f"nowe zadanie: {(s.get('task') or {}).get('title') or title}"
    if action == "create_blocker":
        return f"nowy bloker: {(s.get('blocker') or {}).get('title') or title}"
    if action == "comment":
        return f"komentarz w zadaniu: {title}"
    return None


def report_view(r: dict, dd: DailyData) -> dict:
    """One OS report in the shape the panel, the report sections and MCP already use for Slack reports."""
    items = [i for i in r.get("items") or [] if isinstance(i, dict)]
    lists: dict[str, list[str]] = {v: [] for v in STATE_KEYS.values()}
    outside_items = []
    for it in items:
        text = _title(dd, it) if it.get("kind") == "task" else (it.get("title") or "")
        note = (it.get("note") or "").strip()
        line = f"{text} — {note}" if note and text else text or note
        if not line:
            continue
        if it.get("kind") == "text":
            outside_items.append(text)
            line += " (poza OS)"
        lists[STATE_KEYS.get(it.get("state"), "not_touched")].append(line)
    outside = [x.strip() for x in (r.get("extra") or "").splitlines() if x.strip()]
    needs = [t for t in (_need_text(dd, n) for n in r.get("needs") or [] if isinstance(n, dict)) if t]
    plan = [t for t in (_title(dd, p) for p in r.get("plan") or [] if isinstance(p, dict)) if t]
    suggestions = [s for s in r.get("suggestions") or [] if isinstance(s, dict)]
    analysis = r.get("analysis") or {}
    submitted = r.get("status") == "submitted"
    data = {
        "summary": (analysis.get("summary") or "").strip() if analysis.get("status") == "done" else "",
        "done": lists["done"],
        "in_progress": lists["in_progress"],
        "waiting": lists["waiting"],
        "not_touched": lists["not_touched"],
        "next": plan,
        "needs": needs,
        # the card's "blockada" badge and the report's blokery: explicit requests + tasks waiting on someone
        "blockers": needs + [f"czeka: {w}" for w in lists["waiting"]],
        "outside_os": outside,              # "Poza OS / notatki" lines
        "outside_os_items": outside_items,  # hand-typed items (already in done/in progress/…)
        "plan_quality": ("clear" if plan else "missing") if submitted else None,
        "focus": "",
        "attention": "",
        "os_linked": True,
        "os_checked": True,
        "applied": [t for t in (_applied(dd, s) for s in suggestions if s.get("status") == "applied") if t],
        "pending_suggestions": sum(1 for s in suggestions if s.get("status") == "pending"),
        "analysis_status": analysis.get("status") or "idle",
        "edited_after_submit": bool(r.get("reopenedAt")) or int(analysis.get("runs") or 0) > 1,
        "manager_replied": bool(r.get("comments")),
        "absent": r.get("status") == "off",
    }
    return {
        "source": "os",
        "person_key": f"os:{r.get('authorId')}",
        "person_name": dd.name(r.get("authorId")),
        "os_person_id": r.get("authorId"),
        "day": date.fromisoformat(r["date"]),
        "status": r.get("status"),
        "channels": [],
        "first_message_at": _parse_ts(r.get("submittedAt")),
        "link": f"{dd.app_url.rstrip('/')}/daily?r={r.get('id')}",
        "data": data,
    }


def cutoff_for(day: date) -> datetime:
    return datetime.combine(next_workday(day), MISSING_CUTOFF, tzinfo=WARSAW)


def missing_for(day: date, dd: DailyData, current: datetime | None = None) -> tuple[str, list[dict], list[dict]]:
    """(status, missing, not_yet) by the OS rule (doc §4.2): participants, Polish workdays, submitted/off,
    decided from 10:00 of the next workday. not_yet = participants still without a report before that."""
    current = current or now()
    if not dd.ok:
        return "os_unavailable", [], []
    if not is_workday(day):
        return "weekend", [], []
    people = dd.participants()
    if not people:
        return "not_required", [], []
    done = dd.reported(day)
    drafts = {r.get("authorId") for r in dd.for_day(day)
              if r.get("status") == "draft" and (r.get("metrics") or {}).get("firstInputAt")}
    lacking = [{"name": dd.name(p), "person_id": p, "state": "szkic" if p in drafts else "brak"}
               for p in people if p not in done]
    lacking.sort(key=lambda m: m["name"])
    if day > current.date():
        return "day_in_progress", [], []
    if current < cutoff_for(day):
        return "day_in_progress", [], lacking
    return "ok", lacking, []


def _words(text: str | None) -> set[str]:
    return {w for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 2}


def _same_need(a: dict, b: dict) -> bool:
    if (a.get("personId") or "") != (b.get("personId") or ""):
        return False
    if not a.get("personId") and (a.get("who") or "").strip().lower() != (b.get("who") or "").strip().lower():
        return False
    if a.get("taskId") and a.get("taskId") == b.get("taskId"):
        return True
    wa, wb = _words(a.get("text")), _words(b.get("text"))
    return not wa and not wb or bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5


def escalations(day: date, dd: DailyData, current: datetime | None = None, os_from: date | None = None) -> list[dict]:
    """Things a manager or the CEO should step in on (doc §4.3), as of `day`.
    os_from: first day the team reports only in OS — earlier days are not counted as missing."""
    current = current or now()
    out: list[dict] = []
    if not dd.ok:
        return out
    by_author: dict[str, list[dict]] = {}
    for r in sorted(dd.rows, key=lambda x: x.get("date") or ""):
        if r.get("status") == "submitted" and r.get("authorId") and (r.get("date") or "") <= day.isoformat():
            by_author.setdefault(r["authorId"], []).append(r)

    for pid, reports in by_author.items():
        last = reports[-1]
        if last.get("date") != day.isoformat():
            continue  # escalations are about the state shown on that day's report
        # a request repeated in consecutive reports: nobody reacted
        for need in last.get("needs") or []:
            first = last["date"]
            for prev in reversed(reports[:-1]):
                if any(_same_need(need, n) for n in prev.get("needs") or []):
                    first = prev["date"]
                else:
                    break
            workdays = _workdays_between(date.fromisoformat(first), day)
            if workdays >= NEED_DAYS:
                out.append({"type": "need", "person": dd.name(pid), "person_id": pid,
                            "text": f"{dd.name(pid)} czeka na {_need_text(dd, need)} (od {first}, {workdays} dni rob.)",
                            "since": first})
        # the same task "waiting" in consecutive reports
        for it in last.get("items") or []:
            if it.get("state") != "waiting" or not it.get("taskId"):
                continue
            n = 0
            for rep in reversed(reports):
                if any(x.get("taskId") == it["taskId"] and x.get("state") == "waiting" for x in rep.get("items") or []):
                    n += 1
                else:
                    break
            if n >= WAITING_REPORTS:
                out.append({"type": "waiting", "person": dd.name(pid), "person_id": pid,
                            "text": f"{dd.name(pid)}: „{_title(dd, it)}” czeka od {n} raportów z rzędu",
                            "since": reports[-n]["date"]})

    # workdays in a row without a report (only decided days, only since the team reports in OS)
    for pid in dd.participants():
        streak, d = 0, day
        while dd.start and d >= dd.start and (os_from is None or d >= os_from):
            if not is_workday(d):
                d -= timedelta(days=1)
                continue
            if current < cutoff_for(d) or pid in dd.reported(d):
                break
            streak += 1
            d = previous_workday(d)
        if streak >= MISSING_STREAK:
            out.append({"type": "missing", "person": dd.name(pid), "person_id": pid,
                        "text": f"{dd.name(pid)}: brak raportu {streak} dni robocze z rzędu", "since": None})

    # blockers created from daily reports, open for more than two days
    limit = current - timedelta(days=BLOCKER_DAYS)
    for b in dd.blockers:
        created = _parse_ts(b.get("createdAt"))
        if b.get("status") == "Resolved" or not b.get("sourceDailyId") or not created or created >= limit:
            continue
        owner = dd.name(b.get("ownerId")) if b.get("ownerId") else (b.get("externalParty") or "brak właściciela")
        out.append({"type": "blocker", "person": owner, "person_id": b.get("ownerId"),
                    "text": f"Bloker „{b.get('title')}” otwarty od {(current - created).days} dni ({owner})",
                    "since": created.date().isoformat()})
    return out


def _workdays_between(first: date, last: date) -> int:
    """Workdays after `first` up to and including `last` (same day = 0)."""
    n, d = 0, first
    while d < last:
        d += timedelta(days=1)
        if is_workday(d):
            n += 1
    return n


# ── Slack → OS switch ────────────────────────────────────────────────────────

async def get_cutover(db: Database) -> date | None:
    """From this day on the team reports only in MyBed OS (panel → Zespół). None = transition period."""
    raw = ((await db.get_setting("daily_source", {})) or {}).get("cutover")
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def os_only(day: date, cutover: date | None) -> bool:
    return cutover is not None and day >= cutover
