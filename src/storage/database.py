"""SQLite database for persistent storage (task snapshots, velocity, run logs)."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import aiosqlite

from src.common.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DB_PATH = PROJECT_ROOT / "data" / "agents.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    ran_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL,
    error_message TEXT,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS task_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    asana_task_id TEXT NOT NULL,
    task_name TEXT,
    assignee TEXT,
    project TEXT,
    status TEXT,
    due_date TEXT,
    last_modified TEXT
);

CREATE TABLE IF NOT EXISTS velocity_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,
    person TEXT NOT NULL,
    tasks_closed INTEGER DEFAULT 0,
    tasks_opened INTEGER DEFAULT 0,
    tasks_overdue INTEGER DEFAULT 0,
    UNIQUE(week_start, person)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    summary TEXT,
    body TEXT NOT NULL,
    sources_used TEXT,
    sources_failed TEXT
);
"""


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Initialize database and create tables if needed."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info(f"Database initialized at {self.db_path}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not initialized. Call init() first.")
        return self._db

    async def log_run(
        self,
        agent_name: str,
        status: str,
        error_message: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Log an agent run."""
        await self.db.execute(
            "INSERT INTO agent_runs (agent_name, status, error_message, duration_ms) "
            "VALUES (?, ?, ?, ?)",
            (agent_name, status, error_message, duration_ms),
        )
        await self.db.commit()

    async def save_task_snapshot(
        self,
        asana_task_id: str,
        task_name: str,
        assignee: str | None,
        project: str | None,
        status: str | None,
        due_date: str | None,
        last_modified: str | None,
    ) -> None:
        """Save a point-in-time task snapshot."""
        await self.db.execute(
            "INSERT INTO task_snapshots "
            "(asana_task_id, task_name, assignee, project, status, due_date, last_modified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (asana_task_id, task_name, assignee, project, status, due_date, last_modified),
        )
        await self.db.commit()

    async def update_velocity(
        self, week_start: str, person: str, tasks_closed: int, tasks_overdue: int
    ) -> None:
        """Upsert weekly velocity record."""
        await self.db.execute(
            "INSERT INTO velocity_records (week_start, person, tasks_closed, tasks_overdue) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(week_start, person) DO UPDATE SET "
            "tasks_closed = excluded.tasks_closed, tasks_overdue = excluded.tasks_overdue",
            (week_start, person, tasks_closed, tasks_overdue),
        )
        await self.db.commit()

    async def get_velocity(self, person: str, weeks: int = 4) -> list[dict]:
        """Get velocity records for a person over the last N weeks."""
        cursor = await self.db.execute(
            "SELECT week_start, tasks_closed, tasks_overdue FROM velocity_records "
            "WHERE person = ? ORDER BY week_start DESC LIMIT ?",
            (person, weeks),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_all_velocity(self, weeks: int = 4) -> list[dict]:
        """Get velocity records for all people over the last N weeks."""
        cursor = await self.db.execute(
            "SELECT person, week_start, tasks_closed, tasks_overdue FROM velocity_records "
            "ORDER BY person, week_start DESC LIMIT ?",
            (weeks * 20,),  # rough upper bound
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # --- Reports ---

    async def save_report(
        self,
        agent_name: str,
        summary: str,
        body: str,
        sources_used: list[str] | None = None,
        sources_failed: list[str] | None = None,
    ) -> None:
        """Save a generated report."""
        await self.db.execute(
            "INSERT INTO reports (agent_name, summary, body, sources_used, sources_failed) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                agent_name,
                summary,
                body,
                ",".join(sources_used or []),
                ",".join(sources_failed or []),
            ),
        )
        await self.db.commit()

    async def get_reports(
        self, agent_name: str | None = None, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        """Get reports with optional filtering by agent."""
        if agent_name:
            cursor = await self.db.execute(
                "SELECT * FROM reports WHERE agent_name = ? ORDER BY generated_at DESC LIMIT ? OFFSET ?",
                (agent_name, limit, offset),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM reports ORDER BY generated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_report(self, report_id: int) -> dict | None:
        """Get a single report by ID."""
        cursor = await self.db.execute("SELECT * FROM reports WHERE id = ?", (report_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_reports_count(self, agent_name: str | None = None) -> int:
        """Get total count of reports."""
        if agent_name:
            cursor = await self.db.execute(
                "SELECT COUNT(*) as cnt FROM reports WHERE agent_name = ?", (agent_name,)
            )
        else:
            cursor = await self.db.execute("SELECT COUNT(*) as cnt FROM reports")
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    # --- Runs (for dashboard) ---

    async def get_recent_runs(self, limit: int = 50) -> list[dict]:
        """Get recent agent runs for the logs page."""
        cursor = await self.db.execute(
            "SELECT * FROM agent_runs ORDER BY ran_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_last_run(self, agent_name: str) -> dict | None:
        """Get the most recent run for an agent."""
        cursor = await self.db.execute(
            "SELECT * FROM agent_runs WHERE agent_name = ? ORDER BY ran_at DESC LIMIT 1",
            (agent_name,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
