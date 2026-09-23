"""Cross-source topics — groups events from chats, mail, calendar and OS into business matters.

Existing active topics are given to the model so the same matter keeps one
topic over days instead of a new duplicate every run.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from src.ai.client import AIClient
from src.common.timeutil import now
from src.storage.database import Database, _j

logger = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "existing_topic_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ["product", "operations", "sales", "marketing", "hr", "finance", "logistics", "it", "other"],
                    },
                    "event_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["existing_topic_id", "name", "description", "category", "event_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}

PROMPT = """Grupujesz zdarzenia z firmy MyBed Group (WhatsApp, Gmail, kalendarz) w SPRAWY biznesowe.

Zasady:
- Sprawa to konkretny temat (projekt, problem, klient, dostawa, kampania, rekrutacja), nie kanał ani kategoria.
- Łącz zdarzenia o tej samej sprawie z różnych źródeł.
- Jeśli sprawa pasuje do jednej z ISTNIEJĄCYCH spraw z listy, podaj jej id w existing_topic_id i zaktualizuj opis; nowa sprawa → existing_topic_id = 0.
- Pomiń newslettery, automatyczne powiadomienia, small talk.
- Maksymalnie 15 spraw. Nazwa: maks. 6 słów, po polsku. Opis: 1–2 zdania o aktualnym stanie sprawy.
- event_ids: wszystkie id zdarzeń z tej paczki należące do sprawy."""


class TopicExtractor:
    def __init__(self, db: Database, ai_client: AIClient | None = None):
        self.db = db
        self.ai = ai_client or AIClient(db=db)

    async def extract(self, lookback_hours: int = 24) -> dict:
        stats = {"topics": 0, "events_linked": 0}
        since = now() - timedelta(hours=lookback_hours)
        events = [e for e in await self.db.get_events(since=since, limit=600) if e.get("source") != "asana"]
        if not events:
            return stats

        compact = []
        for e in events:
            meta = _j(e.get("metadata"), {})
            if e["source"] == "gmail" and meta.get("bulk"):
                continue
            compact.append({
                "id": e["id"],
                "src": e["source"],
                "title": (e.get("title") or "")[:120],
                "body": (e.get("body") or "")[:280],
                "from": "Dominik" if meta.get("from_me") else (e.get("sender_name") or meta.get("sender_name") or ""),
            })
        existing = await self.db.get_active_topics(limit=40)
        content = (
            "ISTNIEJĄCE SPRAWY:\n"
            + json.dumps([{"id": t["id"], "name": t["name"], "description": t.get("description")} for t in existing],
                         ensure_ascii=False)
            + "\n\nZDARZENIA:\n"
            + json.dumps(compact, ensure_ascii=False)
        )
        result = await self.ai.extract(
            system=PROMPT, content=content, schema=SCHEMA, max_tokens=8000, effort="medium", purpose="topics",
        )
        valid = {e["id"] for e in compact}
        existing_ids = {t["id"] for t in existing}
        for t in result.get("topics", []):
            ids = [i for i in t.get("event_ids", []) if i in valid]
            if not ids:
                continue
            tid = t.get("existing_topic_id") or 0
            if tid in existing_ids:
                await self.db._execute(
                    "UPDATE topics SET description = $2, last_activity_at = NOW() WHERE id = $1",
                    tid, t.get("description"),
                )
            else:
                tid = await self.db.upsert_topic(t["name"], t.get("description"), t.get("category"))
                await self.db._execute("UPDATE topics SET last_activity_at = NOW() WHERE id = $1", tid)
            for eid in ids:
                await self.db.link_event_to_topic(tid, eid)
            stats["topics"] += 1
            stats["events_linked"] += len(ids)
        logger.info(f"Topic extraction: {stats}")
        return stats
