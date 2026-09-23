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

CREATE INDEX IF NOT EXISTS idx_events_wa_chat ON events ((metadata->>'chat_jid')) WHERE source = 'whatsapp';
CREATE INDEX IF NOT EXISTS idx_events_gmail_thread ON events ((metadata->>'thread_id')) WHERE source = 'gmail';

ALTER TABLE reports ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS html TEXT;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS meta JSONB DEFAULT '{}';
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS details JSONB;

-- Key/value settings edited from the panel (report profiles, preferences)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- AI digest of one chat for one day (Europe/Warsaw)
CREATE TABLE IF NOT EXISTS chat_digests (
    id SERIAL PRIMARY KEY,
    chat_jid TEXT NOT NULL,
    chat_name TEXT,
    is_group BOOLEAN DEFAULT FALSE,
    day DATE NOT NULL,
    summary TEXT,
    importance TEXT DEFAULT 'normal',
    needs_reply BOOLEAN DEFAULT FALSE,
    reply_hint TEXT,
    decisions JSONB DEFAULT '[]',
    open_questions JSONB DEFAULT '[]',
    topics JSONB DEFAULT '[]',
    message_count INTEGER DEFAULT 0,
    last_message_at TIMESTAMPTZ,
    last_event_id INTEGER,
    generated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(chat_jid, day)
);
CREATE INDEX IF NOT EXISTS idx_chat_digests_day ON chat_digests(day);

-- Promises and asks detected in chats / e-mail
CREATE TABLE IF NOT EXISTS commitments (
    id SERIAL PRIMARY KEY,
    fingerprint TEXT UNIQUE NOT NULL,
    direction TEXT NOT NULL,
    title TEXT NOT NULL,
    context TEXT,
    counterpart TEXT,
    source TEXT NOT NULL,
    chat_jid TEXT,
    chat_name TEXT,
    source_at TIMESTAMPTZ,
    due_date DATE,
    due_hint TEXT,
    status TEXT DEFAULT 'open',
    os_ref TEXT,
    os_pushed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status);

-- Claude API usage (cost visibility)
CREATE TABLE IF NOT EXISTS ai_calls (
    id SERIAL PRIMARY KEY,
    at TIMESTAMPTZ DEFAULT NOW(),
    purpose TEXT,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    duration_ms INTEGER,
    ok BOOLEAN DEFAULT TRUE
);

-- OAuth tokens issued to MCP clients (stored hashed)
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    client_id TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    revoked BOOLEAN DEFAULT FALSE
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
        title: str | None = None,
        html: str | None = None,
        meta: dict | None = None,
    ) -> int:
        row = await self._fetchone(
            "INSERT INTO reports (agent_name, summary, body, sources_used, sources_failed, title, html, meta) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
            agent_name, summary, body,
            ",".join(sources_used or []),
            ",".join(sources_failed or []),
            title, html, json.dumps(meta or {}, default=str),
        )
        return row["id"]

    async def update_report_meta(self, report_id: int, patch: dict) -> None:
        await self._execute(
            "UPDATE reports SET meta = COALESCE(meta, '{}'::jsonb) || $2::jsonb WHERE id = $1",
            report_id, json.dumps(patch, default=str),
        )

    async def get_latest_report(self, agent_name: str) -> dict | None:
        return await self._fetchone(
            "SELECT * FROM reports WHERE agent_name = $1 ORDER BY generated_at DESC LIMIT 1", agent_name
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
        upsert: bool = False,
    ) -> int | None:
        """Store a normalized event. Returns event ID, or None if it was a duplicate.

        With upsert=True an existing row is updated in place (calendar events move).
        """
        conflict = (
            "ON CONFLICT (source, source_id) DO UPDATE SET timestamp = EXCLUDED.timestamp, "
            "title = EXCLUDED.title, body = EXCLUDED.body, metadata = EXCLUDED.metadata "
            if upsert else "ON CONFLICT (source, source_id) DO NOTHING "
        )
        try:
            row = await self._fetchone(
                "INSERT INTO events "
                "(source, source_id, event_type, timestamp, title, body, "
                "sender_entity_id, priority, category, sentiment, content_hash, metadata) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) "
                + conflict +
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
        """Resolve a name from a source to an entity ID.

        Order: exact alias for the source → exact display/canonical name →
        first name, but only when exactly one entity has it (so "Ola" never
        matches "Karolina", and two Patryks stay unresolved).
        """
        name = (name or "").strip()
        if not name:
            return None
        row = await self._fetchone(
            "SELECT entity_id FROM entity_aliases WHERE source = $1 AND lower(alias_name) = lower($2) LIMIT 1",
            source, name,
        )
        if row:
            return row["entity_id"]
        row = await self._fetchone(
            "SELECT id FROM entities WHERE lower(display_name) = lower($1) OR lower(canonical_name) = lower($1) "
            "ORDER BY id LIMIT 1",
            name,
        )
        if row:
            return row["id"]
        first = name.split()[0]
        rows = await self._fetchall(
            "SELECT id FROM entities WHERE lower(split_part(display_name, ' ', 1)) = lower($1) LIMIT 2",
            first,
        )
        return rows[0]["id"] if len(rows) == 1 else None

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
        """Reuse an active topic with the same name (case-insensitive) or create one."""
        row = await self._fetchone(
            "SELECT id FROM topics WHERE status = 'active' AND lower(name) = lower($1) "
            "ORDER BY id LIMIT 1",
            name,
        )
        if row:
            if description:
                await self._execute(
                    "UPDATE topics SET description = $2, category = COALESCE($3, category) WHERE id = $1",
                    row["id"], description, category,
                )
            return row["id"]
        row = await self._fetchone(
            "INSERT INTO topics (name, description, category, keywords) VALUES ($1, $2, $3, $4) RETURNING id",
            name, description, category, keywords or [],
        )
        return row["id"]

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
            "  json_build_object('id', ea.id, 'source', ea.source, 'alias', ea.alias_name) "
            "  ORDER BY ea.source"
            ") FILTER (WHERE ea.id IS NOT NULL), '[]'::json) as aliases, "
            "(SELECT COUNT(*) FROM events ev WHERE ev.sender_entity_id = e.id) as event_count "
            "FROM entities e "
            "LEFT JOIN entity_aliases ea ON ea.entity_id = e.id "
            "WHERE e.entity_type = 'person' "
            "GROUP BY e.id "
            "ORDER BY e.canonical_name"
        )

    async def delete_alias(self, alias_id: int) -> None:
        """Delete an entity alias by ID."""
        await self._execute("DELETE FROM entity_aliases WHERE id = $1", alias_id)

    async def resolve_unlinked_events(
        self, source: str, alias_name: str, entity_id: int
    ) -> None:
        """Retroactively link unlinked events where sender matches the new alias."""
        if source == "gmail":
            await self._execute(
                "UPDATE events SET sender_entity_id = $1 "
                "WHERE source = 'gmail' AND sender_entity_id IS NULL "
                "AND (metadata->>'from' ILIKE '%' || $2 || '%' "
                "OR metadata->>'sender_name' ILIKE '%' || $2 || '%')",
                entity_id, alias_name,
            )
        elif source == "whatsapp":
            await self._execute(
                "UPDATE events SET sender_entity_id = $1 "
                "WHERE source = 'whatsapp' AND sender_entity_id IS NULL "
                "AND metadata->>'sender_name' = $2",
                entity_id, alias_name,
            )
        elif source == "asana":
            await self._execute(
                "UPDATE events SET sender_entity_id = $1 "
                "WHERE source = 'asana' AND sender_entity_id IS NULL "
                "AND metadata->>'assignee' ILIKE '%' || $2 || '%'",
                entity_id, alias_name,
            )
        else:
            await self._execute(
                "UPDATE events SET sender_entity_id = $1 "
                "WHERE source = $2 AND sender_entity_id IS NULL "
                "AND (title ILIKE '%' || $3 || '%' OR body ILIKE '%' || $3 || '%')",
                entity_id, source, alias_name,
            )

    # =========================================================================
    # Settings (key/value JSON edited from the panel)
    # =========================================================================

    async def get_setting(self, key: str, default=None):
        row = await self._fetchone("SELECT value FROM settings WHERE key = $1", key)
        if not row:
            return default
        return _j(row["value"], default)

    async def set_setting(self, key: str, value) -> None:
        await self._execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            key, json.dumps(value, ensure_ascii=False, default=str),
        )

    async def settings_fingerprint(self) -> str:
        row = await self._fetchone("SELECT MAX(updated_at)::text AS v, COUNT(*) AS n FROM settings")
        return f"{row['v']}:{row['n']}" if row else ""

    # =========================================================================
    # WhatsApp: chats and messages
    # =========================================================================

    async def get_whatsapp_last_ts(self) -> dict[str, int]:
        """Newest stored message per chat (unix seconds) — the ingestor resumes from here."""
        rows = await self._fetchall(
            "SELECT metadata->>'chat_jid' AS jid, EXTRACT(EPOCH FROM MAX(timestamp))::bigint AS ts "
            "FROM events WHERE source = 'whatsapp' GROUP BY 1"
        )
        return {r["jid"]: int(r["ts"]) for r in rows if r["jid"]}

    async def get_chat_messages(
        self, jid: str, since: datetime | None = None, until: datetime | None = None, limit: int = 300,
    ) -> list[dict]:
        """Messages of one chat in chronological order (newest `limit`)."""
        rows = await self._fetchall(
            "SELECT * FROM ("
            "  SELECT e.id, e.timestamp, e.body, e.metadata, e.sender_entity_id, "
            "  ent.display_name AS sender_display "
            "  FROM events e LEFT JOIN entities ent ON ent.id = e.sender_entity_id "
            "  WHERE e.source = 'whatsapp' AND e.metadata->>'chat_jid' = $1 "
            "  AND ($2::timestamptz IS NULL OR e.timestamp >= $2) "
            "  AND ($3::timestamptz IS NULL OR e.timestamp < $3) "
            "  ORDER BY e.timestamp DESC LIMIT $4"
            ") t ORDER BY timestamp ASC",
            jid, since, until, limit,
        )
        for r in rows:
            r["metadata"] = _j(r.get("metadata"), {})
        return rows

    async def get_active_chats(self, since: datetime, until: datetime | None = None) -> list[dict]:
        """Chats with messages in [since, until), excluding disabled ones."""
        return await self._fetchall(
            "SELECT e.metadata->>'chat_jid' AS jid, "
            "MAX(e.metadata->>'chat_name') AS chat_name, "
            "BOOL_OR(e.category = 'group') AS is_group, "
            "COUNT(*) AS message_count, MAX(e.id) AS last_event_id, MAX(e.timestamp) AS last_message_at, "
            "BOOL_OR(COALESCE((e.metadata->>'from_me')::boolean, false)) AS i_wrote "
            "FROM events e LEFT JOIN whatsapp_chat_config c ON c.jid = e.metadata->>'chat_jid' "
            "WHERE e.source = 'whatsapp' AND e.timestamp >= $1 "
            "AND ($2::timestamptz IS NULL OR e.timestamp < $2) "
            "AND c.enabled IS DISTINCT FROM FALSE "
            "GROUP BY 1 ORDER BY MAX(e.timestamp) DESC",
            since, until,
        )

    async def get_whatsapp_chat(self, jid: str) -> dict | None:
        return await self._fetchone("SELECT * FROM whatsapp_chat_config WHERE jid = $1", jid)

    async def rename_whatsapp_chat_events(self, jid: str, name: str) -> None:
        """Keep event titles in sync when the bridge learns a better chat name."""
        await self._execute(
            "UPDATE events SET title = $2, metadata = metadata || jsonb_build_object('chat_name', $2::text) "
            "WHERE source = 'whatsapp' AND metadata->>'chat_jid' = $1 AND title IS DISTINCT FROM $2",
            jid, name,
        )

    # =========================================================================
    # Inbox: things waiting for my reply
    # =========================================================================

    async def get_whatsapp_awaiting(self, days: int = 5, limit: int = 30) -> list[dict]:
        """Chats where the other side wrote last (direct) or mentioned me (groups) and I haven't answered."""
        return await self._fetchall(
            "WITH recent AS ("
            "  SELECT e.id, e.timestamp, e.body, e.category, e.metadata, "
            "  e.metadata->>'chat_jid' AS jid, COALESCE((e.metadata->>'from_me')::boolean, false) AS from_me, "
            "  COALESCE((e.metadata->>'mentions_me')::boolean, false) AS mentions_me "
            "  FROM events e WHERE e.source = 'whatsapp' AND e.timestamp > NOW() - make_interval(days => $1)"
            "), my_last AS ("
            "  SELECT jid, MAX(timestamp) AS ts FROM recent WHERE from_me GROUP BY jid"
            "), pending AS ("
            "  SELECT r.* FROM recent r LEFT JOIN my_last m ON m.jid = r.jid "
            "  WHERE NOT r.from_me AND (m.ts IS NULL OR r.timestamp > m.ts) "
            "  AND (r.category = 'direct' OR r.mentions_me)"
            ") "
            "SELECT p.jid, MAX(p.metadata->>'chat_name') AS chat_name, BOOL_OR(p.category = 'group') AS is_group, "
            "COUNT(*) AS pending_count, MIN(p.timestamp) AS waiting_since, MAX(p.timestamp) AS last_at, "
            "(ARRAY_AGG(p.body ORDER BY p.timestamp DESC))[1] AS last_body, "
            "(ARRAY_AGG(p.metadata->>'sender_name' ORDER BY p.timestamp DESC))[1] AS last_sender "
            "FROM pending p LEFT JOIN whatsapp_chat_config c ON c.jid = p.jid "
            "WHERE c.enabled IS DISTINCT FROM FALSE "
            "GROUP BY p.jid ORDER BY MIN(p.timestamp) ASC LIMIT $2",
            days, limit,
        )

    async def get_email_awaiting(self, days: int = 3, limit: int = 30) -> list[dict]:
        """Incoming inbox e-mails (not promotions/newsletters) with no later reply of mine in the thread."""
        rows = await self._fetchall(
            "SELECT DISTINCT ON (e.metadata->>'thread_id') e.id, e.timestamp, e.title, e.body, e.metadata, "
            "e.priority, ent.display_name AS sender_display "
            "FROM events e LEFT JOIN entities ent ON ent.id = e.sender_entity_id "
            "WHERE e.source = 'gmail' AND e.timestamp > NOW() - make_interval(days => $1) "
            "AND NOT COALESCE((e.metadata->>'from_me')::boolean, false) "
            "AND e.metadata->'labels' ? 'INBOX' "
            "AND NOT (e.metadata->'labels' ?| ARRAY['CATEGORY_PROMOTIONS','CATEGORY_SOCIAL','CATEGORY_UPDATES','CATEGORY_FORUMS','SPAM','TRASH']) "
            "AND NOT COALESCE((e.metadata->>'bulk')::boolean, false) "
            "AND NOT EXISTS (SELECT 1 FROM events s WHERE s.source = 'gmail' "
            "  AND s.metadata->>'thread_id' = e.metadata->>'thread_id' "
            "  AND COALESCE((s.metadata->>'from_me')::boolean, false) AND s.timestamp > e.timestamp) "
            "ORDER BY e.metadata->>'thread_id', e.timestamp DESC",
            days,
        )
        for r in rows:
            r["metadata"] = _j(r.get("metadata"), {})
        rows.sort(key=lambda r: (r.get("priority") != "high", r["timestamp"]))
        return rows[:limit]

    # =========================================================================
    # Chat digests
    # =========================================================================

    async def upsert_chat_digest(self, d: dict) -> None:
        await self._execute(
            "INSERT INTO chat_digests (chat_jid, chat_name, is_group, day, summary, importance, needs_reply, "
            "reply_hint, decisions, open_questions, topics, message_count, last_message_at, last_event_id, generated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,NOW()) "
            "ON CONFLICT (chat_jid, day) DO UPDATE SET chat_name = EXCLUDED.chat_name, summary = EXCLUDED.summary, "
            "importance = EXCLUDED.importance, needs_reply = EXCLUDED.needs_reply, reply_hint = EXCLUDED.reply_hint, "
            "decisions = EXCLUDED.decisions, open_questions = EXCLUDED.open_questions, topics = EXCLUDED.topics, "
            "message_count = EXCLUDED.message_count, last_message_at = EXCLUDED.last_message_at, "
            "last_event_id = EXCLUDED.last_event_id, generated_at = NOW()",
            d["chat_jid"], d.get("chat_name"), d.get("is_group", False), d["day"], d.get("summary"),
            d.get("importance", "normal"), d.get("needs_reply", False), d.get("reply_hint"),
            json.dumps(d.get("decisions", []), ensure_ascii=False),
            json.dumps(d.get("open_questions", []), ensure_ascii=False),
            json.dumps(d.get("topics", []), ensure_ascii=False),
            d.get("message_count", 0), d.get("last_message_at"), d.get("last_event_id"),
        )

    async def get_digest_marks(self, day: date) -> dict[str, int]:
        rows = await self._fetchall("SELECT chat_jid, last_event_id FROM chat_digests WHERE day = $1", day)
        return {r["chat_jid"]: r["last_event_id"] or 0 for r in rows}

    async def get_chat_digests(
        self, since_day: date, until_day: date | None = None, jid: str | None = None, limit: int = 200,
    ) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM chat_digests WHERE day >= $1 AND ($2::date IS NULL OR day <= $2) "
            "AND ($3::text IS NULL OR chat_jid = $3) "
            "ORDER BY day DESC, CASE importance WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, "
            "last_message_at DESC NULLS LAST LIMIT $4",
            since_day, until_day, jid, limit,
        )
        for r in rows:
            for k in ("decisions", "open_questions", "topics"):
                r[k] = _j(r.get(k), [])
        return rows

    # =========================================================================
    # Commitments
    # =========================================================================

    async def upsert_commitment(self, c: dict) -> int | None:
        row = await self._fetchone(
            "INSERT INTO commitments (fingerprint, direction, title, context, counterpart, source, chat_jid, "
            "chat_name, source_at, due_date, due_hint) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
            "ON CONFLICT (fingerprint) DO UPDATE SET context = EXCLUDED.context, "
            "due_date = COALESCE(EXCLUDED.due_date, commitments.due_date), updated_at = NOW() "
            "RETURNING id",
            c["fingerprint"], c["direction"], c["title"], c.get("context"), c.get("counterpart"),
            c["source"], c.get("chat_jid"), c.get("chat_name"), c.get("source_at"),
            c.get("due_date"), c.get("due_hint"),
        )
        return row["id"] if row else None

    async def list_commitments(
        self, status: str | None = "open", direction: str | None = None, limit: int = 100,
    ) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM commitments WHERE ($1::text IS NULL OR status = $1) "
            "AND ($2::text IS NULL OR direction = $2) "
            "ORDER BY (due_date IS NULL), due_date, source_at DESC NULLS LAST LIMIT $3",
            status, direction, limit,
        )

    async def get_commitment(self, cid: int) -> dict | None:
        return await self._fetchone("SELECT * FROM commitments WHERE id = $1", cid)

    async def set_commitment_status(self, cid: int, status: str) -> None:
        await self._execute(
            "UPDATE commitments SET status = $2, updated_at = NOW() WHERE id = $1", cid, status
        )

    async def mark_commitment_pushed(self, cid: int, os_ref: str) -> None:
        await self._execute(
            "UPDATE commitments SET os_ref = $2, os_pushed_at = NOW(), updated_at = NOW() WHERE id = $1",
            cid, os_ref,
        )

    async def count_commitments(self) -> dict:
        row = await self._fetchone(
            "SELECT COUNT(*) FILTER (WHERE status = 'open') AS open, "
            "COUNT(*) FILTER (WHERE status = 'open' AND direction IN ('mine','ask')) AS mine, "
            "COUNT(*) FILTER (WHERE status = 'open' AND due_date < CURRENT_DATE) AS overdue "
            "FROM commitments"
        )
        return dict(row) if row else {"open": 0, "mine": 0, "overdue": 0}

    # =========================================================================
    # AI usage
    # =========================================================================

    async def log_ai_call(
        self, purpose: str, model: str, input_tokens: int, output_tokens: int,
        cache_read_tokens: int = 0, duration_ms: int | None = None, ok: bool = True,
    ) -> None:
        await self._execute(
            "INSERT INTO ai_calls (purpose, model, input_tokens, output_tokens, cache_read_tokens, duration_ms, ok) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            purpose, model, input_tokens, output_tokens, cache_read_tokens, duration_ms, ok,
        )

    async def get_ai_usage(self, days: int = 30) -> list[dict]:
        return await self._fetchall(
            "SELECT model, purpose, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens "
            "FROM ai_calls WHERE at > NOW() - make_interval(days => $1) GROUP BY 1, 2 ORDER BY 1, 2",
            days,
        )

    # =========================================================================
    # OAuth tokens (MCP)
    # =========================================================================

    async def store_oauth_token(self, token_hash: str, kind: str, client_id: str | None, ttl_s: int) -> None:
        await self._execute(
            "INSERT INTO oauth_tokens (token_hash, kind, client_id, expires_at) "
            "VALUES ($1, $2, $3, NOW() + make_interval(secs => $4))",
            token_hash, kind, client_id, ttl_s,
        )

    async def check_oauth_token(self, token_hash: str, kind: str) -> dict | None:
        return await self._fetchone(
            "SELECT * FROM oauth_tokens WHERE token_hash = $1 AND kind = $2 AND NOT revoked AND expires_at > NOW()",
            token_hash, kind,
        )

    async def revoke_oauth_token(self, token_hash: str) -> None:
        await self._execute("UPDATE oauth_tokens SET revoked = TRUE WHERE token_hash = $1", token_hash)

    async def revoke_all_oauth_tokens(self) -> None:
        await self._execute("UPDATE oauth_tokens SET revoked = TRUE WHERE NOT revoked")

    async def count_oauth_clients(self) -> int:
        row = await self._fetchone(
            "SELECT COUNT(DISTINCT client_id) AS n FROM oauth_tokens "
            "WHERE kind = 'refresh' AND NOT revoked AND expires_at > NOW()"
        )
        return row["n"] if row else 0

    # =========================================================================
    # Misc for the panel
    # =========================================================================

    async def prune_calendar_window(self, start: datetime, end: datetime, keep_ids: set[str]) -> None:
        """Drop calendar events in the synced window that no longer exist in Google."""
        await self._execute(
            "DELETE FROM events WHERE source = 'calendar' AND timestamp >= $1 AND timestamp < $2 "
            "AND NOT (source_id = ANY($3::text[]))",
            start, end, list(keep_ids),
        )

    async def get_calendar_events(self, start: datetime, end: datetime) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM events WHERE source = 'calendar' AND timestamp >= $1 AND timestamp < $2 "
            "ORDER BY timestamp",
            start, end,
        )
        for r in rows:
            r["metadata"] = _j(r.get("metadata"), {})
        return rows

    async def get_last_run_any(self, names: list[str]) -> dict[str, dict]:
        rows = await self._fetchall(
            "SELECT DISTINCT ON (agent_name) * FROM agent_runs WHERE agent_name = ANY($1) "
            "ORDER BY agent_name, ran_at DESC",
            names,
        )
        return {r["agent_name"]: r for r in rows}

    async def last_ingest_at(self, source: str) -> datetime | None:
        row = await self._fetchone(
            "SELECT MAX(ingested_at) AS ts FROM events WHERE source = $1", source
        )
        return row["ts"] if row else None

    async def close_stale_topics(self, days: int = 14) -> None:
        await self._execute(
            "UPDATE topics SET status = 'closed' WHERE status = 'active' "
            "AND COALESCE(last_activity_at, created_at) < NOW() - make_interval(days => $1)",
            days,
        )


def _j(value, default=None):
    """Decode a JSONB value returned as text by asyncpg."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
