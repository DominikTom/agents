"""Task Monitor Agent - tracks Asana tasks, calculates velocity, detects bottlenecks."""

from __future__ import annotations

import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path

from src.agents.base import BaseAgent
from src.common.types import AgentReport, ConnectorResult
from src.connectors import create_connector
from src.storage.models import PersonVelocityProfile, VelocityRecord

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "task_monitor.txt"


class TaskMonitorAgent(BaseAgent):
    name = "task_monitor"

    async def gather_data(self) -> dict[str, ConnectorResult]:
        """Fetch current tasks from Asana."""
        connector = create_connector("asana", self.config)
        if connector is None:
            return {"asana": ConnectorResult(source="asana", error="Asana connector not available")}

        result = await connector.fetch()
        return {"asana": result}

    async def analyze(self, data: dict[str, ConnectorResult]) -> AgentReport:
        """Analyze tasks, compute velocity, generate report via Claude Haiku."""
        asana_data = data.get("asana")
        if not asana_data or not asana_data.ok:
            return AgentReport(
                agent_name=self.name,
                summary="Asana data unavailable",
                body="Could not fetch Asana tasks. Check connector configuration.",
            )

        # Store snapshots for velocity tracking
        for task in asana_data.items:
            await self.db.save_task_snapshot(
                asana_task_id=task.get("task_id", ""),
                task_name=task.get("name", ""),
                assignee=task.get("assignee"),
                project=task.get("project"),
                status=task.get("status"),
                due_date=task.get("due_date"),
                last_modified=task.get("modified_at"),
            )

        # Compute velocity per person
        velocity_profiles = await self._compute_velocity(asana_data.items)

        # Build context for Claude
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

        context_parts = [
            f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"\n=== CURRENT TASKS ({len(asana_data.items)} items) ===",
            json.dumps(asana_data.items, ensure_ascii=False, indent=2, default=str),
        ]

        if velocity_profiles:
            context_parts.append("\n=== VELOCITY (historical data) ===")
            for profile in velocity_profiles:
                context_parts.append(
                    f"{profile.person}: avg {profile.avg_tasks_closed_per_week} tasks/week "
                    f"(trend: {profile.trend}, tracked {profile.weeks_tracked} weeks, "
                    f"avg overdue: {profile.avg_tasks_overdue_per_week}/week)"
                )

        context = "\n".join(context_parts)

        # Use Haiku for cost efficiency
        agents_config = self.config.get("agents", {}).get("task_monitor", {})
        model = agents_config.get("model", "claude-haiku-4-5-20251001")
        max_tokens = agents_config.get("max_tokens", 2048)

        response = await self.ai.summarize(
            context=context,
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
        )

        lines = response.strip().split("\n")
        summary = "\n".join(lines[:3])

        return AgentReport(
            agent_name=self.name,
            summary=summary,
            body=response,
        )

    async def _compute_velocity(self, current_tasks: list[dict]) -> list[PersonVelocityProfile]:
        """Compute velocity profiles from historical data."""
        agents_config = self.config.get("agents", {}).get("task_monitor", {})
        weeks = agents_config.get("velocity_window_weeks", 4)

        # Get unique assignees from current tasks
        assignees = set()
        for task in current_tasks:
            assignee = task.get("assignee")
            if assignee and assignee != "Unassigned":
                assignees.add(assignee)

        # Update velocity for current week
        today = date.today()
        week_start = (today - timedelta(days=today.weekday())).isoformat()

        for assignee in assignees:
            person_tasks = [t for t in current_tasks if t.get("assignee") == assignee]
            overdue = sum(1 for t in person_tasks if t.get("status") == "overdue")
            # Note: closed tasks counted from Asana completed_at in a future enhancement
            await self.db.update_velocity(week_start, assignee, tasks_closed=0, tasks_overdue=overdue)

        # Build profiles from historical data
        profiles = []
        for assignee in assignees:
            records_raw = await self.db.get_velocity(assignee, weeks)
            records = [
                VelocityRecord(
                    person=assignee,
                    week_start=r["week_start"],
                    tasks_closed=r["tasks_closed"],
                    tasks_overdue=r["tasks_overdue"],
                )
                for r in records_raw
            ]
            profile = PersonVelocityProfile.from_records(assignee, records)
            profiles.append(profile)

        return profiles
