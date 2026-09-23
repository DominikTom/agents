"""MyBed Group OS (Supabase `os_entities`) — tasks, projects, blockers, decisions, people.

Replaces Asana. Read-mostly; the only writes are private tasks / AI suggestions
created from commitments the CEO approved in the agents panel.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, timezone

from src.common.config import get_env_optional
from src.common.timeutil import today
from src.connectors.supabase_rest import SupabaseREST

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://fvfydervumwznlopqbsn.supabase.co"
DEFAULT_APP_URL = "https://os.mybed.cloud"

OPEN_TASK = {"Todo", "Doing", "Waiting", "Blocked"}
CLOSED_PROJECT = {"Done", "Cancelled", "Paused", "Idea"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OSClient:
    def __init__(self):
        self.rest = SupabaseREST(
            get_env_optional("OS_SUPABASE_URL") or DEFAULT_URL,
            get_env_optional("OS_SUPABASE_SERVICE_KEY"),
        )
        self.app_url = (get_env_optional("OS_APP_URL") or DEFAULT_APP_URL).rstrip("/")
        self.owner_id = get_env_optional("OS_OWNER_PERSON_ID") or "p-dominik"

    @property
    def configured(self) -> bool:
        return self.rest.configured

    async def collection(self, name: str) -> list[dict]:
        rows = await self.rest.select(
            "os_entities", {"select": "data", "collection": f"eq.{name}", "order": "id"}
        )
        return [r["data"] for r in rows if r.get("data")]

    async def snapshot(self) -> "OSSnapshot":
        names = ["tasks", "projects", "blockers", "decisions", "people", "milestones"]
        results = await asyncio.gather(*(self.collection(n) for n in names))
        return OSSnapshot(dict(zip(names, results)), self.owner_id, self.app_url)

    def link(self, kind: str, entity_id: str) -> str:
        return f"{self.app_url}/?p={kind}:{entity_id}"

    async def create_private_task(self, title: str, description: str, due: date | None = None) -> str:
        """Task assigned to the owner, visible only to them (RLS: visibility=custom)."""
        tid = "task-ag-" + hashlib.sha1(f"{title}{_now_iso()}".encode()).hexdigest()[:10]
        item = {
            "id": tid,
            "title": title[:200],
            "description": description,
            "status": "Todo",
            "priority": "P2",
            "dueDate": due.isoformat() if due else None,
            "ownerId": self.owner_id,
            "assigneeId": self.owner_id,
            "createdById": self.owner_id,
            "createdAt": _now_iso(),
            "updatedAt": _now_iso(),
            "visibility": "custom",
            "allowedPersonIds": [self.owner_id],
            "allowedDepartmentIds": [],
            "confidentiality": "confidential",
            "tags": [],
            "comments": [],
            "checklist": [],
            "attachments": [],
            "followerIds": [],
            "projectId": None,
            "goalId": None,
            "milestoneId": None,
            "parentTaskId": None,
            "reviewerId": None,
            "estimateHours": None,
            "actualHours": None,
        }
        await self.rest.upsert("os_entities", [{"collection": "tasks", "id": tid, "data": item}], "collection,id")
        return tid

    async def create_suggestion(self, title: str, description: str, summary: str, source: str) -> str:
        """Pending suggestion in the OS inbox. NOTE: visible to every OS member."""
        sid = "sug-ag-" + hashlib.sha1(f"{title}{_now_iso()}".encode()).hexdigest()[:10]
        item = {
            "id": sid,
            "at": _now_iso(),
            "createdAt": _now_iso(),
            "action": "create_task",
            "source": source,
            "status": "pending",
            "summary": summary[:300],
            "targetId": None,
            "targetTitle": title[:200],
            "targetCollection": "tasks",
            "proposedTask": {"title": title[:200], "description": description, "assigneeName": ""},
            "proposedUpdate": description[:1000],
            "resolvedAt": None,
            "resolvedBy": None,
        }
        await self.rest.upsert(
            "os_entities", [{"collection": "aiSuggestions", "id": sid, "data": item}], "collection,id"
        )
        return sid


class OSSnapshot:
    """In-memory view over OS collections with the derived lists reports need."""

    def __init__(self, data: dict[str, list[dict]], owner_id: str, app_url: str):
        self.data = data
        self.owner_id = owner_id
        self.app_url = app_url
        self.people = {p["id"]: p for p in data.get("people", [])}
        self.projects = {p["id"]: p for p in data.get("projects", [])}
        self.today = today().isoformat()

    def name(self, pid: str | None) -> str:
        return (self.people.get(pid) or {}).get("name", "") if pid else ""

    def _task_view(self, t: dict) -> dict:
        proj = self.projects.get(t.get("projectId") or "")
        return {
            "title": t.get("title", ""),
            "status": t.get("status"),
            "priority": t.get("priority"),
            "due": t.get("dueDate"),
            "assignee": self.name(t.get("assigneeId") or t.get("ownerId")),
            "project": proj.get("title") if proj else None,
            "link": f"{self.app_url}/?p=task:{t.get('id')}",
        }

    @property
    def open_tasks(self) -> list[dict]:
        return [t for t in self.data.get("tasks", []) if t.get("status") in OPEN_TASK and not t.get("parentTaskId")]

    def my_tasks(self) -> dict:
        mine = [t for t in self.open_tasks if (t.get("assigneeId") or t.get("ownerId")) == self.owner_id]
        overdue = [t for t in mine if t.get("dueDate") and t["dueDate"] < self.today]
        due_today = [t for t in mine if t.get("dueDate") == self.today]
        upcoming = sorted(
            [t for t in mine if t.get("dueDate") and t["dueDate"] > self.today], key=lambda t: t["dueDate"]
        )[:10]
        return {
            "overdue": [self._task_view(t) for t in overdue],
            "today": [self._task_view(t) for t in due_today],
            "upcoming": [self._task_view(t) for t in upcoming],
            "open_count": len(mine),
        }

    def team_load(self) -> list[dict]:
        """Per person: open, overdue, due today, blocked/waiting — sorted by overdue."""
        rows: dict[str, dict] = {}
        for t in self.open_tasks:
            pid = t.get("assigneeId") or t.get("ownerId")
            if not pid or pid == self.owner_id:
                continue
            r = rows.setdefault(pid, {"person": self.name(pid), "open": 0, "overdue": 0, "today": 0, "stuck": 0, "overdue_titles": []})
            r["open"] += 1
            due = t.get("dueDate")
            if due and due < self.today:
                r["overdue"] += 1
                if len(r["overdue_titles"]) < 4:
                    r["overdue_titles"].append(f"{t.get('title')} (termin {due})")
            elif due == self.today:
                r["today"] += 1
            if t.get("status") in {"Blocked", "Waiting"}:
                r["stuck"] += 1
        return sorted((r for r in rows.values() if r["person"]), key=lambda r: (-r["overdue"], -r["open"]))

    def blockers(self) -> list[dict]:
        out = []
        for b in self.data.get("blockers", []):
            if b.get("status") == "Resolved":
                continue
            proj = self.projects.get(b.get("relatedProjectId") or "")
            out.append({
                "title": b.get("title"),
                "severity": b.get("severity"),
                "owner": self.name(b.get("ownerId")),
                "reason": b.get("blockingReason") or b.get("description"),
                "next_step": b.get("nextStep"),
                "project": proj.get("title") if proj else None,
                "since": (b.get("createdAt") or "")[:10],
            })
        return out

    def projects_attention(self) -> list[dict]:
        """Active projects that are red/yellow, blocked, overdue, flagged for CEO or stale."""
        out = []
        for p in self.data.get("projects", []):
            if p.get("status") in CLOSED_PROJECT:
                continue
            reasons = []
            if p.get("ceoAttention"):
                reasons.append("oznaczony do uwagi CEO")
            if p.get("decisionNeeded"):
                reasons.append("wymaga decyzji")
            if p.get("health") in {"red", "yellow"}:
                reasons.append(f"health: {p['health']}")
            if p.get("status") in {"Blocked", "Waiting for Input"}:
                reasons.append(f"status: {p['status']}")
            target = p.get("targetDate") or p.get("dueDate")
            if target and target < self.today:
                reasons.append(f"po terminie ({target})")
            last = (p.get("lastUpdateAt") or p.get("updatedAt") or "")[:10]
            if p.get("status") == "In Progress" and last and last < _days_ago(14):
                reasons.append(f"brak update od {last}")
            if reasons:
                out.append({
                    "title": p.get("title"),
                    "owner": self.name(p.get("ownerId")),
                    "status": p.get("status"),
                    "progress": p.get("progress"),
                    "reasons": reasons,
                    "link": f"{self.app_url}/?p=project:{p.get('id')}",
                })
        return out

    def decisions_pending(self) -> list[dict]:
        return [
            {"title": d.get("title"), "context": (d.get("context") or "")[:300], "decider": self.name(d.get("deciderId"))}
            for d in self.data.get("decisions", []) if d.get("status") == "Proposed"
        ]

    def completed_since(self, since: date) -> list[dict]:
        s = since.isoformat()
        done = [
            t for t in self.data.get("tasks", [])
            if t.get("status") == "Done" and (t.get("updatedAt") or "")[:10] >= s
        ]
        return [self._task_view(t) for t in done]

    def projects_active(self) -> list[dict]:
        return [
            {"title": p.get("title"), "owner": self.name(p.get("ownerId")), "status": p.get("status"),
             "health": p.get("health"), "progress": p.get("progress"), "target": p.get("targetDate")}
            for p in self.data.get("projects", []) if p.get("status") not in CLOSED_PROJECT
        ]

    def summary_counts(self) -> dict:
        mine = self.my_tasks()
        team = self.team_load()
        return {
            "my_overdue": len(mine["overdue"]),
            "my_today": len(mine["today"]),
            "my_open": mine["open_count"],
            "team_overdue": sum(r["overdue"] for r in team),
            "blockers": len(self.blockers()),
            "attention": len(self.projects_attention()),
            "decisions": len(self.decisions_pending()),
        }


def _days_ago(n: int) -> str:
    from datetime import timedelta
    return (today() - timedelta(days=n)).isoformat()
