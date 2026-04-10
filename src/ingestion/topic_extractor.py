"""Topic extraction - AI-powered identification of cross-source business topics.

Periodically scans recent events, uses Claude Haiku to identify key topics,
and stores them in the topics + topic_events tables.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.ai.client import AIClient
from src.storage.database import Database

logger = logging.getLogger(__name__)

WARSAW = ZoneInfo("Europe/Warsaw")

EXTRACTION_PROMPT = """Analyze the following business events from the last 24 hours. Identify the KEY BUSINESS TOPICS being discussed across different channels (email, WhatsApp, Asana tasks, calendar).

RULES:
1. A "topic" is a concrete business matter (project, issue, deal, event) - NOT a source or category
2. MERGE events that discuss the same topic across sources into ONE topic
3. Focus on topics that appear in 2+ sources or are clearly important
4. Skip newsletters, automated notifications, generic greetings
5. Maximum 15 topics, minimum 3 (if enough data)
6. For each topic, list ALL event IDs that relate to it

Return ONLY valid JSON array, no other text:
[
  {
    "name": "Short topic name (max 6 words)",
    "description": "1-2 sentence summary of what's happening with this topic",
    "category": "product|operations|sales|marketing|hr|finance|logistics|other",
    "event_ids": [1, 2, 3],
    "priority": "high|normal|low"
  }
]"""


class TopicExtractor:
    """Extracts cross-source business topics from recent events using AI."""

    def __init__(self, db: Database, ai_client: AIClient | None = None):
        self.db = db
        self.ai = ai_client or AIClient()

    async def extract(self, lookback_hours: int = 24) -> dict:
        """Run topic extraction on recent events. Returns stats."""
        stats = {"topics_created": 0, "topics_updated": 0, "events_linked": 0, "errors": 0}

        try:
            since = datetime.now(WARSAW) - timedelta(hours=lookback_hours)
            events = await self.db.get_events(since=since, limit=500)

            if not events:
                logger.info("Topic extraction: no events to process")
                return stats

            # Build compact event summaries for AI (save tokens)
            event_summaries = []
            for e in events:
                meta = e.get("metadata", {})
                if isinstance(meta, str):
                    meta = json.loads(meta)

                summary = {
                    "id": e["id"],
                    "source": e.get("source", ""),
                    "title": e.get("title", ""),
                    "body": (e.get("body") or "")[:300],
                    "sender": e.get("sender_name") or meta.get("sender_name", ""),
                    "timestamp": e["timestamp"].isoformat() if e.get("timestamp") else "",
                }

                # Add source-specific context
                if e.get("source") == "asana":
                    summary["assignee"] = meta.get("assignee", "")
                    summary["status"] = meta.get("status", "")
                    summary["project"] = meta.get("project", "")
                elif e.get("source") == "gmail":
                    summary["from_me"] = meta.get("from_me", False)
                elif e.get("source") == "whatsapp":
                    summary["from_me"] = meta.get("from_me", False)
                    summary["chat"] = meta.get("chat_name", "")

                event_summaries.append(summary)

            context = json.dumps(event_summaries, ensure_ascii=False, default=str)

            # Call Claude Haiku for cheap topic extraction
            response = await self.ai.summarize(
                context=context,
                system_prompt=EXTRACTION_PROMPT,
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
            )

            # Parse JSON response
            topics = self._parse_topics(response)
            if not topics:
                logger.warning("Topic extraction: AI returned no valid topics")
                return stats

            # Valid event IDs for linking
            valid_ids = {e["id"] for e in events}

            # Store topics and link events
            for topic_data in topics:
                try:
                    topic_id = await self.db.upsert_topic(
                        name=topic_data["name"],
                        description=topic_data.get("description", ""),
                        category=topic_data.get("category", "other"),
                        keywords=[],
                    )

                    # Update last_activity
                    await self.db._execute(
                        "UPDATE topics SET last_activity_at = NOW() WHERE id = $1",
                        topic_id,
                    )

                    if topic_id:
                        stats["topics_created"] += 1
                        for eid in topic_data.get("event_ids", []):
                            if eid in valid_ids:
                                await self.db.link_event_to_topic(topic_id, eid)
                                stats["events_linked"] += 1

                except Exception as e:
                    logger.warning(f"Topic store error: {e}")
                    stats["errors"] += 1

            logger.info(
                f"Topic extraction: {stats['topics_created']} topics, "
                f"{stats['events_linked']} event links"
            )

        except Exception as e:
            logger.error(f"Topic extraction failed: {e}")
            stats["errors"] += 1

        return stats

    def _parse_topics(self, response: str) -> list[dict]:
        """Parse AI response to topic list."""
        # Find JSON array in response
        text = response.strip()
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1:
            return []
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            logger.warning(f"Topic extraction: failed to parse JSON")
            return []
