"""PostgreSQL database for Business Knowledge Base.

Replaces SQLite with asyncpg for concurrent access from agents, MCP server, and dashboard.
Contains both legacy tables (agent_runs, reports, etc.) and new Knowledge Base tables
(events, entities, topics, summaries, business_metrics).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date, timedelta

import asyncpg

from src.common.config import get_env, get_env_optional

logger = logging.getLogger(__name__)


def _dsn() -> str:
    """Build PostgreSQL connection string from env."""
    host = get_env_optional("POSTGRES_HOST") or "postgres"
    port = get_env_optional("POSTGRES_PORT") or "5432"
    db = get_env_optional("POSTGRES_DB") or "agents"
    user = get_env_optional("POSTGRES_USER") or "agents"
    password = get_env("POSTGRES_PASSWORD")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


# --- Schema ---

SCHEMA_SQL = """
-- Legacy tables (migrated from SQLite)

CREATE TABLE IF NOT EXISTS agent_runs (
    id SERIAL PRIMARY KEY,
    agent_name TEXT NOT NULL,
    ran_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL,
    error_message TEXT,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS task_snapshots (
    id SERIAL PRIMARY KEY,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    asana_task_id TEXT NOT NULL,
    task_name TEXT,
    assignee TEXT,
    project TEXT,
    status TEXT,
    due_date TEXT,
    last_modified TEXT
);

CREATE TABLE IF NOT EXISTS velocity_records (
    id SERIAL PRIMARY KEY,
    week_start TEXT NOT NULL,
    person TEXT NOT NULL,
    tasks_closed INTEGER DEFAULT 0,
    tasks_opened INTEGER DEFAULT 0,
    tasks_overdue INTEGER DEFAULT 0,
    UNIQUE(week_start, person)
);

CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    agent_name TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    summary TEXT,
    body TEXT NOT NULL,
    sources_used TEXT,
    sources_failed TEXT
);

-- Knowledge Base: Entities (people, companies, projects)

CREATE TABLE IF NOT EXISTS entities (
    id SERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(entity_type, canonical_name)
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id SERIAL PRIMARY KEY,
    entity_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    external_id TEXT,
    alias_name TEXT NOT NULL,
    UNIQUE(source, alias_name)
);

-- Knowledge Base: Events (normalized from all sources)

CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT,
    event_type TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ DEFAULT NOW(),
    title TEXT,
    body TEXT,
    sender_entity_id INTEGER REFERENCES entities(id),
    priority TEXT DEFAULT 'normal',
    category TEXT,
    sentiment TEXT,
    content_hash TEXT,
    metadata JSONB DEFAULT '{}',
    search_vector TSVECTOR,
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
CREATE INDEX IF NOT EXISTS idx_events_priority ON events(priority);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);
CREATE INDEX IF NOT EXISTS idx_events_sender ON events(sender_entity_id);
CREATE INDEX IF NOT EXISTS idx_events_search ON events USING GIN(search_vector);

-- Auto-update search vector
CREATE OR REPLACE FUNCTION events_search_trigger() RETURNS trigger AS $$
BEGIN
  NEW.search_vector := to_tsvector('simple', coalesce(NEW.title, '') || ' ' || coalesce(NEW.body, ''));
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_search_update ON events;
CREATE TRIGGER events_search_update BEFORE INSERT OR UPDATE ON events
FOR EACH ROW EXECUTE FUNCTION events_search_trigger();

-- Knowledge Base: Event-Entity links

CREATE TABLE IF NOT EXISTS event_entities (
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    entity_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    role TEXT DEFAULT 'mentioned',
    PRIMARY KEY(event_id, entity_id, role)
);

-- Knowledge Base: Topics (cross-source threads)

CREATE TABLE IF NOT EXISTS topics (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'active',
    category TEXT,
    keywords TEXT[],
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_activity_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS topic_events (
    topic_id INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    PRIMARY KEY(topic_id, event_id)
);

-- Knowledge Base: Summaries (institutional memory)

CREATE TABLE IF NOT EXISTS summaries (
    id SERIAL PRIMARY KEY,
    period_type TEXT NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    generated_at TIMESTAMPTZ DEFAULT NOW(),
    summary_text TEXT NOT NULL,
    key_metrics JSONB,
    open_items JSONB,
    events_count INTEGER,
    UNIQUE(period_type, period_start)
);

-- Knowledge Base: Business Metrics (time-series KPIs)

CREATE TABLE IF NOT EXISTS business_metrics (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    store TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value NUMERIC NOT NULL,
    currency TEXT,
    metadata JSONB DEFAULT '{}',
    UNIQUE(date, store, metric_name)
);

CREATE INDEX IF NOT EXISTS idx_metrics_date ON business_metrics(date);
CREATE INDEX IF NOT EXISTS idx_metrics_store ON business_metrics(store);

-- WhatsApp chat configuration (enable/disable per chat)

CREATE TABLE IF NOT EXISTS whatsapp_chat_config (
    jid TEXT PRIMARY KEY,
    chat_name TEXT NOT NULL,
    is_group BOOLEAN DEFAULT FALSE,
    enabled BOOLEAN DEFAULT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""


class Database:
    """Async PostgreSQL database wrapper."""

    def __init__(self):
        self._pool: asyncpg.Pool | None = None

    async def init(self, run_schema: bool = True) -> None:
        """Initialize connection pool and optionally create tables."""
        dsn = _dsn()
        self._pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)

        if run_schema:
            async with self._pool.acquire() as conn:
                await conn.execute(SCHEMA_SQL)

        logger.info("PostgreSQL database initialized")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not initialized. Call init() first.")
        return self._pool

    # --- Helper ---

    async def _fetchall(self, query: str, *args) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [dict(row) for row in rows]

    async def _fetchone(self, query: str, *args) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return dict(row) if row else None

    async def _execute(self, query: str, *args) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(query, *args)

    # =========================================================================
    # Legacy methods (backward compatible with existing agents/dashboard)
    # =========================================================================

    async def log_run(
        self,
        agent_name: str,
        status: str,
        error_message: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        await self._execute(
            "INSERT INTO agent_runs (agent_name, status, error_message, duration_ms) "
            "VALUES ($1, $2, $3, $4)",
            agent_name, status, error_message, duration_ms,
        )

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
        await self._execute(
            "INSERT INTO task_snapshots "
            "(asana_task_id, task_name, assignee, project, status, due_date, last_modified) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            asana_task_id, task_name, assignee, project, status, due_date, last_modified,
        )

    async def update_velocity(
        self, week_start: str, person: str, tasks_closed: int, tasks_overdue: int
    ) -> None:
        await self._execute(
            "INSERT INTO velocity_records (week_start, person, tasks_closed, tasks_overdue) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT(week_start, person) DO UPDATE SET "
            "tasks_closed = EXCLUDED.tasks_closed, tasks_overdue = EXCLUDED.tasks_overdue",
            week_start, person, tasks_closed, tasks_overdue,
        )

    async def get_velocity(self, person: str, weeks: int = 4) -> list[dict]:
        return await self._fetchall(
            "SELECT week_start, tasks_closed, tasks_overdue FROM velocity_records "
            "WHERE person = $1 ORDER BY week_start DESC LIMIT $2",
            person, weeks,
        )

    async def get_all_velocity(self, weeks: int = 4) -> list[dict]:
        return await self._fetchall(
            "SELECT person, week_start, tasks_closed, tasks_overdue FROM velocity_records "
            "ORDER BY person, week_start DESC LIMIT $1",
            weeks * 20,
        )

    # --- Reports ---

    async def save_report(
        self,
        agent_name: str,
        summary: str,
        body: str,
        sources_used: list[str] | None = None,
        sources_failed: list[str] | None = None,
    ) -> None:
        await self._execute(
            "INSERT INTO reports (agent_name, summary, body, sources_used, sources_failed) "
            "VALUES ($1, $2, $3, $4, $5)",
            agent_name, summary, body,
            ",".join(sources_used or []),
            ",".join(sources_failed or []),
        )

    async def get_reports(
        self, agent_name: str | None = None, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        if agent_name:
            return await self._fetchall(
                "SELECT * FROM reports WHERE agent_name = $1 ORDER BY generated_at DESC LIMIT $2 OFFSET $3",
                agent_name, limit, offset,
            )
        return await self._fetchall(
            "SELECT * FROM reports ORDER BY generated_at DESC LIMIT $1 OFFSET $2",
            limit, offset,
        )

    async def get_report(self, report_id: int) -> dict | None:
        return await self._fetchone("SELECT * FROM reports WHERE id = $1", report_id)

    async def get_reports_count(self, agent_name: str | None = None) -> int:
        if agent_name:
            row = await self._fetchone(
                "SELECT COUNT(*) as cnt FROM reports WHERE agent_name = $1", agent_name
            )
        else:
            row = await self._fetchone("SELECT COUNT(*) as cnt FROM reports")
        return row["cnt"] if row else 0

    # --- Runs (dashboard) ---

    async def get_recent_runs(self, limit: int = 50) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM agent_runs ORDER BY ran_at DESC LIMIT $1", limit
        )

    async def get_last_run(self, agent_name: str) -> dict | None:
        return await self._fetchone(
            "SELECT * FROM agent_runs WHERE agent_name = $1 ORDER BY ran_at DESC LIMIT 1",
            agent_name,
        )

    # =========================================================================
    # Knowledge Base: Events
    # =========================================================================

    async def store_event(
        self,
        source: str,
        source_id: str,
        event_type: str,
        timestamp: datetime,
        title: str | None = None,
        body: str | None = None,
        sender_entity_id: int | None = None,
        priority: str = "normal",
        category: str | None = None,
        sentiment: str | None = None,
        content_hash: str | None = None,
        metadata: dict | None = None,
    ) -> int | None:
        """Store a normalized event. Returns event ID or None if duplicate."""
        try:
            row = await self._fetchone(
                "INSERT INTO events "
                "(source, source_id, event_type, timestamp, title, body, "
                "sender_entity_id, priority, category, sentiment, content_hash, metadata) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) "
                "ON CONFLICT (source, source_id) DO NOTHING "
                "RETURNING id",
                source, source_id, event_type, timestamp, title, body,
                sender_entity_id, priority, category, sentiment, content_hash,
                json.dumps(metadata or {}),
            )
            return row["id"] if row else None
        except Exception as e:
            logger.warning(f"Failed to store event {source}/{source_id}: {e}")
            return None

    async def get_events(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        source: str | None = None,
        priority: list[str] | None = None,
        category: str | None = None,
        sender_entity_id: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Query events with flexible filtering."""
        conditions = []
        params = []
        idx = 1

        if since:
            conditions.append(f"timestamp >= ${idx}")
            params.append(since)
            idx += 1
        if until:
            conditions.append(f"timestamp <= ${idx}")
            params.append(until)
            idx += 1
        if source:
            conditions.append(f"source = ${idx}")
            params.append(source)
            idx += 1
        if priority:
            conditions.append(f"priority = ANY(${idx})")
            params.append(priority)
            idx += 1
        if category:
            conditions.append(f"category = ${idx}")
            params.append(category)
            idx += 1
        if sender_entity_id:
            conditions.append(f"sender_entity_id = ${idx}")
            params.append(sender_entity_id)
            idx += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)

        return await self._fetchall(
            f"SELECT e.*, ent.display_name as sender_name "
            f"FROM events e "
            f"LEFT JOIN entities ent ON e.sender_entity_id = ent.id "
            f"{where} "
            f"ORDER BY e.timestamp DESC LIMIT ${idx}",
            *params,
        )

    async def search_events(self, query: str, limit: int = 50) -> list[dict]:
        """Full-text search across events."""
        return await self._fetchall(
            "SELECT e.*, ent.display_name as sender_name, "
            "ts_rank(search_vector, plainto_tsquery('simple', $1)) as rank "
            "FROM events e "
            "LEFT JOIN entities ent ON e.sender_entity_id = ent.id "
            "WHERE search_vector @@ plainto_tsquery('simple', $1) "
            "ORDER BY rank DESC LIMIT $2",
            query, limit,
        )

    async def count_events(self, since: datetime | None = None, source: str | None = None) -> int:
        conditions = []
        params = []
        idx = 1
        if since:
            conditions.append(f"timestamp >= ${idx}")
            params.append(since)
            idx += 1
        if source:
            conditions.append(f"source = ${idx}")
            params.append(source)
            idx += 1
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        row = await self._fetchone(f"SELECT COUNT(*) as cnt FROM events {where}", *params)
        return row["cnt"] if row else 0

    # =========================================================================
    # Knowledge Base: Entities
    # =========================================================================

    async def upsert_entity(
        self,
        entity_type: str,
        canonical_name: str,
        display_name: str,
        metadata: dict | None = None,
    ) -> int:
        """Create or update an entity. Returns entity ID."""
        row = await self._fetchone(
            "INSERT INTO entities (entity_type, canonical_name, display_name, metadata) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (entity_type, canonical_name) DO UPDATE SET "
            "display_name = EXCLUDED.display_name, metadata = EXCLUDED.metadata, "
            "updated_at = NOW() "
            "RETURNING id",
            entity_type, canonical_name, display_name, json.dumps(metadata or {}),
        )
        return row["id"]

    async def upsert_alias(
        self, entity_id: int, source: str, alias_name: str, external_id: str | None = None
    ) -> None:
        await self._execute(
            "INSERT INTO entity_aliases (entity_id, source, alias_name, external_id) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (source, alias_name) DO UPDATE SET "
            "entity_id = EXCLUDED.entity_id, external_id = EXCLUDED.external_id",
            entity_id, source, alias_name, external_id,
        )

    async def resolve_entity(self, source: str, name: str) -> int | None:
        """Resolve a name from a source to an entity ID."""
        row = await self._fetchone(
            "SELECT entity_id FROM entity_aliases WHERE source = $1 AND alias_name = $2",
            source, name,
        )
        if row:
            return row["entity_id"]
        # Fuzzy: try matching display_name
        row = await self._fetchone(
            "SELECT id FROM entities WHERE display_name ILIKE $1 OR canonical_name ILIKE $1",
            f"%{name}%",
        )
        return row["id"] if row else None

    async def get_all_entities(self, entity_type: str | None = None) -> list[dict]:
        if entity_type:
            return await self._fetchall(
                "SELECT * FROM entities WHERE entity_type = $1 ORDER BY display_name", entity_type
            )
        return await self._fetchall("SELECT * FROM entities ORDER BY entity_type, display_name")

    # =========================================================================
    # Knowledge Base: Topics
    # =========================================================================

    async def upsert_topic(
        self, name: str, description: str | None = None, category: str | None = None,
        keywords: list[str] | None = None,
    ) -> int:
        row = await self._fetchone(
            "INSERT INTO topics (name, description, category, keywords) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT DO NOTHING RETURNING id",
            name, description, category, keywords or [],
        )
        if row:
            return row["id"]
        row = await self._fetchone("SELECT id FROM topics WHERE name = $1", name)
        return row["id"] if row else 0

    async def link_event_to_topic(self, topic_id: int, event_id: int) -> None:
        await self._execute(
            "INSERT INTO topic_events (topic_id, event_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            topic_id, event_id,
        )

    async def get_active_topics(self, limit: int = 10) -> list[dict]:
        return await self._fetchall(
            "SELECT t.*, COUNT(te.event_id) as event_count "
            "FROM topics t LEFT JOIN topic_events te ON t.id = te.topic_id "
            "WHERE t.status = 'active' "
            "GROUP BY t.id ORDER BY t.last_activity_at DESC NULLS LAST LIMIT $1",
            limit,
        )

    async def get_topic_with_events(self, topic_id: int, limit: int = 50) -> dict | None:
        topic = await self._fetchone("SELECT * FROM topics WHERE id = $1", topic_id)
        if not topic:
            return None
        events = await self._fetchall(
            "SELECT e.*, ent.display_name as sender_name "
            "FROM events e "
            "JOIN topic_events te ON e.id = te.event_id "
            "LEFT JOIN entities ent ON e.sender_entity_id = ent.id "
            "WHERE te.topic_id = $1 ORDER BY e.timestamp DESC LIMIT $2",
            topic_id, limit,
        )
        return {**topic, "events": events}

    # =========================================================================
    # Knowledge Base: Summaries
    # =========================================================================

    async def save_summary(
        self,
        period_type: str,
        period_start: date,
        period_end: date,
        summary_text: str,
        key_metrics: dict | None = None,
        open_items: list | None = None,
        events_count: int | None = None,
    ) -> None:
        await self._execute(
            "INSERT INTO summaries "
            "(period_type, period_start, period_end, summary_text, key_metrics, open_items, events_count) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (period_type, period_start) DO UPDATE SET "
            "summary_text = EXCLUDED.summary_text, key_metrics = EXCLUDED.key_metrics, "
            "open_items = EXCLUDED.open_items, events_count = EXCLUDED.events_count, "
            "generated_at = NOW()",
            period_type, period_start, period_end, summary_text,
            json.dumps(key_metrics or {}), json.dumps(open_items or []), events_count,
        )

    async def get_summary(self, period_type: str, period_start: date) -> dict | None:
        return await self._fetchone(
            "SELECT * FROM summaries WHERE period_type = $1 AND period_start = $2",
            period_type, period_start,
        )

    async def get_recent_summaries(self, period_type: str = "daily", limit: int = 7) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM summaries WHERE period_type = $1 ORDER BY period_start DESC LIMIT $2",
            period_type, limit,
        )

    # =========================================================================
    # Knowledge Base: Business Metrics
    # =========================================================================

    async def store_metric(
        self, metric_date: date, store: str, metric_name: str, metric_value: float,
        currency: str | None = None, metadata: dict | None = None,
    ) -> None:
        await self._execute(
            "INSERT INTO business_metrics (date, store, metric_name, metric_value, currency, metadata) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (date, store, metric_name) DO UPDATE SET "
            "metric_value = EXCLUDED.metric_value, currency = EXCLUDED.currency",
            metric_date, store, metric_name, metric_value, currency,
            json.dumps(metadata or {}),
        )

    # =========================================================================
    # WhatsApp Chat Config
    # =========================================================================

    async def upsert_whatsapp_chat(self, jid: str, chat_name: str, is_group: bool) -> None:
        """Register a chat from the bridge. Does not overwrite enabled status."""
        await self._execute(
            "INSERT INTO whatsapp_chat_config (jid, chat_name, is_group) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (jid) DO UPDATE SET chat_name = EXCLUDED.chat_name, is_group = EXCLUDED.is_group",
            jid, chat_name, is_group,
        )

    async def set_whatsapp_chat_enabled(self, jid: str, enabled: bool | None) -> None:
        await self._execute(
            "UPDATE whatsapp_chat_config SET enabled = $2, updated_at = NOW() WHERE jid = $1",
            jid, enabled,
        )

    async def get_whatsapp_chat_configs(self) -> list[dict]:
        """Get all chats with their enabled status, merged with message stats."""
        return await self._fetchall(
            "SELECT c.jid, c.chat_name, c.is_group, c.enabled, "
            "COALESCE(s.total_messages, 0) as total_messages, "
            "COALESCE(s.today, 0) as today, "
            "s.last_message_at, "
            "cn.contact_name "
            "FROM whatsapp_chat_config c "
            "LEFT JOIN ("
            "  SELECT metadata->>'chat_jid' as jid, "
            "  COUNT(*) as total_messages, "
            "  COUNT(*) FILTER (WHERE timestamp >= NOW() - INTERVAL '1 day') as today, "
            "  MAX(timestamp) as last_message_at "
            "  FROM events WHERE source = 'whatsapp' GROUP BY metadata->>'chat_jid'"
            ") s ON c.jid = s.jid "
            "LEFT JOIN LATERAL ("
            "  SELECT metadata->>'sender_name' as contact_name "
            "  FROM events "
            "  WHERE source = 'whatsapp' AND metadata->>'chat_jid' = c.jid "
            "  AND (metadata->>'from_me')::boolean IS NOT TRUE "
            "  AND metadata->>'sender_name' IS NOT NULL "
            "  AND metadata->>'sender_name' != '' "
            "  GROUP BY metadata->>'sender_name' "
            "  ORDER BY COUNT(*) DESC LIMIT 1"
            ") cn ON NOT c.is_group "
            "ORDER BY s.last_message_at DESC NULLS LAST, c.chat_name"
        )

    async def get_disabled_chat_jids(self) -> set[str]:
        """Get JIDs of explicitly disabled chats."""
        rows = await self._fetchall(
            "SELECT jid FROM whatsapp_chat_config WHERE enabled = FALSE"
        )
        return {r["jid"] for r in rows}

    # =========================================================================
    # Knowledge Base: WhatsApp Monitor
    # =========================================================================

    async def get_recent_whatsapp_messages(self, limit: int = 50) -> list[dict]:
        """Get most recent WhatsApp messages for dashboard."""
        return await self._fetchall(
            "SELECT e.id, e.timestamp, e.title as chat_name, e.body, "
            "e.category as chat_type, e.metadata, "
            "ent.display_name as sender_name "
            "FROM events e "
            "LEFT JOIN entities ent ON e.sender_entity_id = ent.id "
            "WHERE e.source = 'whatsapp' "
            "ORDER BY e.timestamp DESC LIMIT $1",
            limit,
        )

    async def get_whatsapp_chat_stats(self) -> list[dict]:
        """Get message counts per chat for WhatsApp."""
        return await self._fetchall(
            "SELECT title as chat_name, category as chat_type, "
            "COUNT(*) as total_messages, "
            "COUNT(*) FILTER (WHERE timestamp >= NOW() - INTERVAL '1 day') as today, "
            "COUNT(*) FILTER (WHERE timestamp >= NOW() - INTERVAL '7 days') as this_week, "
            "MAX(timestamp) as last_message_at "
            "FROM events WHERE source = 'whatsapp' "
            "GROUP BY title, category "
            "ORDER BY last_message_at DESC"
        )

    async def get_unmapped_whatsapp_senders(self) -> list[dict]:
        """Get WhatsApp sender names that are not mapped to entities."""
        return await self._fetchall(
            "SELECT DISTINCT metadata->>'sender_name' as sender_name, "
            "COUNT(*) as message_count, "
            "MAX(timestamp) as last_seen "
            "FROM events "
            "WHERE source = 'whatsapp' AND sender_entity_id IS NULL "
            "AND metadata->>'sender_name' IS NOT NULL "
            "AND metadata->>'sender_name' != '' "
            "AND (metadata->>'from_me')::boolean IS NOT TRUE "
            "GROUP BY metadata->>'sender_name' "
            "ORDER BY message_count DESC"
        )

    async def get_whatsapp_sync_status(self) -> dict:
        """Get WhatsApp sync status for dashboard."""
        row = await self._fetchone(
            "SELECT COUNT(*) as total, "
            "MAX(ingested_at) as last_sync, "
            "COUNT(*) FILTER (WHERE ingested_at >= NOW() - INTERVAL '5 minutes') as recent "
            "FROM events WHERE source = 'whatsapp'"
        )
        return dict(row) if row else {"total": 0, "last_sync": None, "recent": 0}

    async def get_metrics(
        self, store: str | None = None, since: date | None = None, until: date | None = None,
    ) -> list[dict]:
        conditions = []
        params = []
        idx = 1
        if store:
            conditions.append(f"store = ${idx}")
            params.append(store)
            idx += 1
        if since:
            conditions.append(f"date >= ${idx}")
            params.append(since)
            idx += 1
        if until:
            conditions.append(f"date <= ${idx}")
            params.append(until)
            idx += 1
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        return await self._fetchall(
            f"SELECT * FROM business_metrics {where} ORDER BY date DESC, store, metric_name",
            *params,
        )

    # =========================================================================
    # Pipeline Dashboard
    # =========================================================================

    async def get_ingestion_stats(self) -> list[dict]:
        """Get ingestion stats per source for pipeline dashboard."""
        return await self._fetchall(
            "SELECT source, "
            "COUNT(*) as total, "
            "COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '24 hours') as last_24h, "
            "COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '1 hour') as last_1h, "
            "MAX(timestamp) as last_event "
            "FROM events GROUP BY source ORDER BY source"
        )

    async def get_entity_map(self) -> list[dict]:
        """Get entity resolution map with cross-source aliases and event counts."""
        return await self._fetchall(
            "SELECT e.id, e.canonical_name, e.display_name, e.entity_type, "
            "COALESCE(json_agg("
            "  json_build_object('source', ea.source, 'alias', ea.alias_name) "
            "  ORDER BY ea.source"
            ") FILTER (WHERE ea.id IS NOT NULL), '[]'::json) as aliases, "
            "(SELECT COUNT(*) FROM events ev WHERE ev.sender_entity_id = e.id) as event_count "
            "FROM entities e "
            "LEFT JOIN entity_aliases ea ON ea.entity_id = e.id "
            "WHERE e.entity_type = 'person' "
            "GROUP BY e.id "
            "ORDER BY e.canonical_name"
        )
