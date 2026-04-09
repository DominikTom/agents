"""Daily Wrap Agent - afternoon summary + tomorrow plan (16:00).

First agent to use data FROM the Knowledge Base (events table) instead of
only live connector fetches. Saves daily summaries for institutional memory.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.agents.base import BaseAgent
from src.common.types import AgentReport, ConnectorResult
from src.connectors import create_connector

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "daily_wrap.txt"
WARSAW = ZoneInfo("Europe/Warsaw")


class DailyWrapAgent(BaseAgent):
    name = "daily_wrap"

    async def gather_data(self) -> dict[str, ConnectorResult]:
        """Gather data primarily from DB (events table) + live calendar for tomorrow."""
        now = datetime.now(WARSAW)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        tomorrow_end = tomorrow_start + timedelta(days=1)
        week_start = today_start - timedelta(days=7)

        results: dict[str, ConnectorResult] = {}

        # --- DB queries (Knowledge Base) ---

        # All emails today (incoming + sent for cross-referencing)
        emails = await self.db.get_events(source="gmail", since=today_start, limit=200)
        results["emails_today"] = ConnectorResult(
            source="emails_today",
            items=self._records_to_items(emails),
        )

        # WhatsApp messages today
        wa_messages = await self.db.get_events(source="whatsapp", since=today_start, limit=300)
        results["whatsapp_today"] = ConnectorResult(
            source="whatsapp_today",
            items=self._records_to_items(wa_messages),
        )

        # Asana tasks (recent activity for pending/overdue context)
        asana_tasks = await self.db.get_events(source="asana", since=week_start, limit=100)
        results["asana_tasks"] = ConnectorResult(
            source="asana_tasks",
            items=self._records_to_items(asana_tasks),
        )

        # Calendar events that happened today (from DB)
        calendar_today = await self.db.get_events(source="calendar", since=today_start, limit=50)
        results["calendar_today"] = ConnectorResult(
            source="calendar_today",
            items=self._records_to_items(calendar_today),
        )

        # Business metrics today
        metrics = await self.db.get_metrics(since=date.today())
        if metrics:
            results["metrics_today"] = ConnectorResult(
                source="metrics_today",
                items=[dict(m) for m in metrics],
            )
        else:
            results["metrics_today"] = ConnectorResult(source="metrics_today", items=[])

        # --- Live connector: Calendar for TOMORROW ---
        try:
            cal_connector = create_connector("google_calendar", self.config)
            if cal_connector:
                cal_result = await cal_connector.fetch()
                # Filter to tomorrow only
                if cal_result.ok and cal_result.items:
                    tomorrow_items = []
                    tomorrow_str = tomorrow_start.strftime("%Y-%m-%d")
                    for item in cal_result.items:
                        start = item.get("start", {})
                        dt = start.get("dateTime", start.get("date", ""))
                        if tomorrow_str in str(dt):
                            tomorrow_items.append(item)
                    results["calendar_tomorrow"] = ConnectorResult(
                        source="calendar_tomorrow",
                        items=tomorrow_items,
                    )
                else:
                    results["calendar_tomorrow"] = ConnectorResult(
                        source="calendar_tomorrow",
                        items=[],
                        error=cal_result.error,
                    )
            else:
                results["calendar_tomorrow"] = ConnectorResult(
                    source="calendar_tomorrow", error="Calendar connector not configured"
                )
        except Exception as e:
            logger.error(f"Calendar fetch for tomorrow failed: {e}")
            results["calendar_tomorrow"] = ConnectorResult(
                source="calendar_tomorrow", error=str(e)
            )

        return results

    async def analyze(self, data: dict[str, ConnectorResult]) -> AgentReport:
        """Send DB-sourced data to Claude for afternoon analysis."""
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

        context_parts = []
        for source_name, result in data.items():
            if result.ok and result.items:
                context_parts.append(
                    f"=== {source_name.upper()} ({len(result.items)} items) ===\n"
                    + json.dumps(result.items, ensure_ascii=False, indent=2, default=str)
                )
            elif result.error:
                context_parts.append(f"=== {source_name.upper()} === NIEDOSTEPNE: {result.error}")
            else:
                context_parts.append(f"=== {source_name.upper()} === Brak danych")

        now = datetime.now(WARSAW)
        context = f"Data: {now.strftime('%Y-%m-%d %H:%M')} (popołudniowe podsumowanie)\n\n"
        context += "\n\n".join(context_parts)

        agents_config = self.config.get("agents", {}).get("daily_wrap", {})
        model = agents_config.get("model", "claude-sonnet-4-20250514")
        max_tokens = agents_config.get("max_tokens", 4096)

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

    async def run(self) -> AgentReport | None:
        """Override run() to also save daily summary to summaries table."""
        report = await super().run()
        if report:
            await self._save_daily_summary(report)
        return report

    async def _save_daily_summary(self, report: AgentReport) -> None:
        """Save to summaries table for institutional memory."""
        try:
            today = date.today()

            # Count events for metadata
            now = datetime.now(WARSAW)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            events_count = await self.db.count_events(since=today_start)

            await self.db.save_summary(
                period_type="daily",
                period_start=today,
                period_end=today,
                summary_text=report.body,
                key_metrics={
                    "sources_used": report.sources_used or [],
                    "sources_failed": report.sources_failed or [],
                },
                open_items=[],
                events_count=events_count,
            )
            logger.info(f"[{self.name}] Saved daily summary for {today}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to save daily summary: {e}")

    @staticmethod
    def _records_to_items(records: list[dict]) -> list[dict]:
        """Convert DB records to JSON-serializable items."""
        items = []
        for r in records:
            item = {}
            for k, v in r.items():
                if isinstance(v, datetime):
                    item[k] = v.isoformat()
                elif isinstance(v, date):
                    item[k] = v.isoformat()
                else:
                    item[k] = v
            items.append(item)
        return items
