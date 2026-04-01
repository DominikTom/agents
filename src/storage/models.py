"""Data models for storage layer - used by Task Monitor for velocity tracking."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TaskSnapshot(BaseModel):
    """Point-in-time snapshot of an Asana task."""

    asana_task_id: str
    task_name: str
    assignee: str | None = None
    project: str | None = None
    status: str | None = None
    due_date: str | None = None
    last_modified: str | None = None
    captured_at: datetime = Field(default_factory=datetime.now)


class VelocityRecord(BaseModel):
    """Weekly velocity metrics for a person."""

    person: str
    week_start: str
    tasks_closed: int = 0
    tasks_opened: int = 0
    tasks_overdue: int = 0

    @property
    def avg_display(self) -> str:
        return f"{self.tasks_closed} tasks/week"


class PersonVelocityProfile(BaseModel):
    """Aggregated velocity profile for a person."""

    person: str
    weeks_tracked: int
    avg_tasks_closed_per_week: float
    avg_tasks_overdue_per_week: float
    trend: str = "stable"  # "up", "down", "stable"
    records: list[VelocityRecord] = Field(default_factory=list)

    @classmethod
    def from_records(cls, person: str, records: list[VelocityRecord]) -> PersonVelocityProfile:
        if not records:
            return cls(person=person, weeks_tracked=0, avg_tasks_closed_per_week=0, avg_tasks_overdue_per_week=0)

        avg_closed = sum(r.tasks_closed for r in records) / len(records)
        avg_overdue = sum(r.tasks_overdue for r in records) / len(records)

        # Simple trend: compare last 2 weeks vs previous 2
        trend = "stable"
        if len(records) >= 4:
            recent = sum(r.tasks_closed for r in records[:2]) / 2
            older = sum(r.tasks_closed for r in records[2:4]) / 2
            if recent > older * 1.2:
                trend = "up"
            elif recent < older * 0.8:
                trend = "down"

        return cls(
            person=person,
            weeks_tracked=len(records),
            avg_tasks_closed_per_week=round(avg_closed, 1),
            avg_tasks_overdue_per_week=round(avg_overdue, 1),
            trend=trend,
            records=records,
        )
