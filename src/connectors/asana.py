"""Asana connector - fetches tasks using Asana REST API directly via httpx."""

from __future__ import annotations

import logging
from datetime import datetime, date

import httpx

from src.common.config import get_env
from src.common.types import ConnectorResult
from src.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

ASANA_BASE_URL = "https://app.asana.com/api/1.0"


class AsanaConnector(BaseConnector):
    name = "asana"

    def _get_headers(self) -> dict:
        token = get_env("ASANA_PERSONAL_ACCESS_TOKEN")
        return {"Authorization": f"Bearer {token}"}

    async def fetch(self, since: datetime | None = None) -> ConnectorResult:
        try:
            headers = self._get_headers()
            workspace_id = get_env("ASANA_WORKSPACE_ID")
            asana_config = self.config.get("connectors", {}).get("asana", {})
            project_ids = asana_config.get("projects", [])

            today = date.today().isoformat()
            items = []
            opt_fields = "name,assignee.name,due_on,completed,modified_at,memberships.project.name"

            async with httpx.AsyncClient(timeout=30.0) as client:
                if project_ids:
                    for project_id in project_ids:
                        response = await client.get(
                            f"{ASANA_BASE_URL}/projects/{project_id}/tasks",
                            headers=headers,
                            params={
                                "opt_fields": opt_fields,
                                "completed_since": "now",
                                "limit": 100,
                            },
                        )
                        response.raise_for_status()
                        tasks = response.json().get("data", [])

                        for task in tasks:
                            if task.get("completed"):
                                continue

                            assignee = task.get("assignee")
                            assignee_name = assignee.get("name", "Unassigned") if assignee else "Unassigned"
                            due = task.get("due_on")

                            project_name = ""
                            memberships = task.get("memberships", [])
                            if memberships:
                                proj = memberships[0].get("project")
                                if proj:
                                    project_name = proj.get("name", "")

                            status = "active"
                            if due and due < today:
                                status = "overdue"
                            elif due == today:
                                status = "due_today"

                            modified = task.get("modified_at", "")
                            if modified:
                                try:
                                    mod_date = datetime.fromisoformat(modified.replace("Z", "+00:00"))
                                    days_stale = (datetime.now(mod_date.tzinfo) - mod_date).days
                                    if days_stale >= asana_config.get("blocked_threshold_days", 3):
                                        status = "blocked"
                                except (ValueError, TypeError):
                                    pass

                            items.append({
                                "task_id": task["gid"],
                                "name": task.get("name", ""),
                                "assignee": assignee_name,
                                "project": project_name,
                                "due_date": due,
                                "status": status,
                                "modified_at": modified,
                            })
                else:
                    # Search workspace for tasks due today or overdue
                    response = await client.get(
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
                    response.raise_for_status()
                    tasks = response.json().get("data", [])

                    for task in tasks:
                        assignee = task.get("assignee")
                        assignee_name = assignee.get("name", "Unassigned") if assignee else "Unassigned"
                        due = task.get("due_on")
                        status = "overdue" if due and due < today else "due_today"

                        items.append({
                            "task_id": task["gid"],
                            "name": task.get("name", ""),
                            "assignee": assignee_name,
                            "due_date": due,
                            "status": status,
                            "modified_at": task.get("modified_at", ""),
                        })

            logger.info(f"Asana: fetched {len(items)} tasks")
            return self._success_result(items)

        except Exception as e:
            logger.error(f"Asana fetch failed: {e}")
            return self._error_result(str(e))
