"""Asana connector - fetches tasks, overdue items, and project status."""

from __future__ import annotations

import logging
from datetime import datetime, date

import asana

from src.common.config import get_env
from src.common.types import ConnectorResult
from src.connectors.base import BaseConnector

logger = logging.getLogger(__name__)


class AsanaConnector(BaseConnector):
    name = "asana"

    def __init__(self, config: dict):
        super().__init__(config)
        self._client = None

    def _get_client(self) -> asana.Client:
        if self._client is None:
            token = get_env("ASANA_PERSONAL_ACCESS_TOKEN")
            self._client = asana.Client.access_token(token)
            self._client.headers = {"asana-enable": "new_goal_memberships,new_user_task_lists"}
        return self._client

    async def fetch(self, since: datetime | None = None) -> ConnectorResult:
        try:
            client = self._get_client()
            workspace_id = get_env("ASANA_WORKSPACE_ID")
            asana_config = self.config.get("connectors", {}).get("asana", {})
            project_ids = asana_config.get("projects", [])

            today = date.today().isoformat()
            items = []

            if project_ids:
                # Fetch tasks from specific projects
                for project_id in project_ids:
                    tasks = client.tasks.get_tasks_for_project(
                        project_id,
                        opt_fields=[
                            "name", "assignee.name", "due_on", "completed",
                            "completed_at", "modified_at", "memberships.project.name",
                            "notes",
                        ],
                    )
                    for task in tasks:
                        if task.get("completed"):
                            continue

                        assignee = task.get("assignee", {})
                        assignee_name = assignee.get("name", "Unassigned") if assignee else "Unassigned"
                        due = task.get("due_on")

                        project_name = ""
                        memberships = task.get("memberships", [])
                        if memberships:
                            project_name = memberships[0].get("project", {}).get("name", "")

                        status = "active"
                        if due and due < today:
                            status = "overdue"
                        elif due == today:
                            status = "due_today"

                        # Detect blocked: no modification in 3+ days
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
                # Fallback: search workspace for tasks due today or overdue
                search_params = {
                    "workspace": workspace_id,
                    "completed": False,
                    "opt_fields": "name,assignee.name,due_on,modified_at,memberships.project.name",
                    "due_on.before": today,
                    "sort_by": "due_date",
                }
                tasks = client.tasks.search_tasks_for_workspace(workspace_id, search_params)
                for task in tasks:
                    assignee = task.get("assignee", {})
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
