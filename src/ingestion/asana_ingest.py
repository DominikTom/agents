"""Asana task ingestion - syncs tasks to PostgreSQL events table."""

from __future__ import annotations

import logging
from datetime import datetime, date, timezone

import httpx

from src.common.config import get_env
from src.storage.database import Database

logger = logging.getLogger(__name__)

ASANA_BASE_URL = "https://app.asana.com/api/1.0"


class AsanaIngestor:
    """Pulls tasks from Asana and stores them as events in PostgreSQL."""

    def __init__(self, db: Database, config: dict):
        self.db = db
        self.config = config

    async def sync(self) -> dict:
        """Sync Asana tasks to DB. Returns stats."""
        stats = {"new_tasks": 0, "updated_tasks": 0, "errors": 0}

        try:
            token = get_env("ASANA_PERSONAL_ACCESS_TOKEN")
            workspace_id = get_env("ASANA_WORKSPACE_ID")
            headers = {"Authorization": f"Bearer {token}"}
            asana_config = self.config.get("connectors", {}).get("asana", {})
            project_ids = asana_config.get("projects", [])

            today = date.today().isoformat()
            opt_fields = "name,assignee.name,due_on,completed,modified_at,memberships.project.name,notes"

            async with httpx.AsyncClient(timeout=30.0) as client:
                tasks = []

                if project_ids:
                    for project_id in project_ids:
                        resp = await client.get(
                            f"{ASANA_BASE_URL}/projects/{project_id}/tasks",
                            headers=headers,
                            params={
                                "opt_fields": opt_fields,
                                "completed_since": "now",
                                "limit": 100,
                            },
                        )
                        resp.raise_for_status()
                        tasks.extend(resp.json().get("data", []))
                else:
                    resp = await client.get(
                        f"{ASANA_BASE_URL}/workspaces/{workspace_id}/tasks/search",
                        headers=headers,
                        params={
                            "opt_fields": opt_fields,
                            "completed": "false",
                            "due_on.before": today,
                            "sort_by": "due_date",
                            "limit": 100,
                        },
                    )
                    resp.raise_for_status()
                    tasks = resp.json().get("data", [])

                for task in tasks:
                    if task.get("completed"):
                        continue

                    try:
                        gid = task["gid"]
                        name = task.get("name", "")
                        assignee = task.get("assignee")
                        assignee_name = assignee.get("name", "Unassigned") if assignee else "Unassigned"
                        due = task.get("due_on")
                        modified = task.get("modified_at", "")
                        notes = task.get("notes", "")

                        project_name = ""
                        memberships = task.get("memberships", [])
                        if memberships:
                            proj = memberships[0].get("project")
                            if proj:
                                project_name = proj.get("name", "")

                        # Determine status
                        status = "active"
                        if due and due < today:
                            status = "overdue"
                        elif due == today:
                            status = "due_today"

                        # Parse modified_at for timestamp
                        if modified:
                            timestamp = datetime.fromisoformat(modified.replace("Z", "+00:00"))
                        else:
                            timestamp = datetime.now(timezone.utc)

                        # Resolve assignee entity
                        sender_entity_id = None
                        if assignee_name and assignee_name != "Unassigned":
                            sender_entity_id = await self.db.resolve_entity("asana", assignee_name)

                        event_id = await self.db.store_event(
                            source="asana",
                            source_id=f"asana:{gid}",
                            event_type="task",
                            timestamp=timestamp,
                            title=name,
                            body=notes[:500] if notes else f"Assignee: {assignee_name}, Due: {due}, Status: {status}",
                            sender_entity_id=sender_entity_id,
                            priority="high" if status == "overdue" else "normal",
                            category=status,
                            metadata={
                                "asana_gid": gid,
                                "assignee": assignee_name,
                                "project": project_name,
                                "due_date": due,
                                "status": status,
                                "modified_at": modified,
                            },
                        )

                        if event_id is not None:
                            stats["new_tasks"] += 1

                    except Exception as e:
                        logger.warning(f"Asana ingest error for task {task.get('gid')}: {e}")
                        stats["errors"] += 1

        except Exception as e:
            logger.error(f"Asana ingestion failed: {e}")
            stats["errors"] += 1

        if stats["new_tasks"] > 0:
            logger.info(f"Asana sync: {stats['new_tasks']} new/updated tasks ingested")

        return stats
