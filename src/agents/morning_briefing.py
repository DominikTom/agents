"""Morning Briefing Agent - daily summary from all data sources."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.agents.base import BaseAgent
from src.common.types import AgentReport, ConnectorResult
from src.connectors import create_connector

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "morning_briefing.txt"
WARSAW = ZoneInfo("Europe/Warsaw")


class MorningBriefingAgent(BaseAgent):
    name = "morning_briefing"

    async def gather_data(self) -> dict[str, ConnectorResult]:
        """Fetch data from connectors + WhatsApp from DB (ingestor has all messages)."""
        agents_config = self.config.get("agents", {}).get("morning_briefing", {})
        connector_names = agents_config.get("connectors", [
            "gmail", "slack", "asana", "google_calendar",
            "google_sheets", "ideaerp",
        ])

        # Remove whatsapp from live connectors - we'll read from DB instead
        connector_names = [n for n in connector_names if n != "whatsapp"]

        connectors = {}
        for name in connector_names:
            connector = create_connector(name, self.config)
            if connector:
                connectors[name] = connector
            else:
                logger.warning(f"Unknown connector: {name}")

        # Run all connectors concurrently
        async def _fetch(name: str, connector):
            try:
                return name, await connector.fetch()
            except Exception as e:
                logger.error(f"Connector {name} crashed: {e}")
                return name, ConnectorResult(source=name, error=str(e))

        tasks = [_fetch(name, conn) for name, conn in connectors.items()]
        results_list = await asyncio.gather(*tasks)

        results = {name: result for name, result in results_list}

        # WhatsApp: read from DB (ingestor stores all messages every 5 min)
        lookback = agents_config.get("email_lookback_hours", 13)
        since = datetime.now(WARSAW) - timedelta(hours=lookback)
        try:
            wa_events = await self.db.get_events(source="whatsapp", since=since, limit=300)
            wa_items = []
            for e in wa_events:
                meta = e.get("metadata", {})
                if isinstance(meta, str):
                    meta = json.loads(meta)
                wa_items.append({
                    "chat": e.get("title", ""),
                    "from": e.get("sender_name") or meta.get("sender_name", ""),
                    "body": e.get("body", ""),
                    "timestamp": e.get("timestamp").isoformat() if e.get("timestamp") else "",
                    "from_me": meta.get("from_me", False),
                })
            results["whatsapp"] = ConnectorResult(source="whatsapp", items=wa_items)
            logger.info(f"WhatsApp from DB: {len(wa_items)} messages (last {lookback}h)")
        except Exception as e:
            logger.error(f"WhatsApp DB read failed: {e}")
            results["whatsapp"] = ConnectorResult(source="whatsapp", error=str(e))

        return results

    async def analyze(self, data: dict[str, ConnectorResult]) -> AgentReport:
        """Send all gathered data to Claude for summarization."""
        # Load system prompt
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

        # Build context from all sources
        context_parts = []
        for source_name, result in data.items():
            if result.ok and result.items:
                context_parts.append(
                    f"=== {source_name.upper()} ({len(result.items)} items) ===\n"
                    + json.dumps(result.items, ensure_ascii=False, indent=2, default=str)
                )
            elif result.error:
                context_parts.append(f"=== {source_name.upper()} === NIEDOSTĘPNE: {result.error}")

        context = f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n" + "\n\n".join(context_parts)

        # Get model config
        agents_config = self.config.get("agents", {}).get("morning_briefing", {})
        model = agents_config.get("model", "claude-sonnet-4-20250514")
        max_tokens = agents_config.get("max_tokens", 4096)

        # Call Claude
        response = await self.ai.summarize(
            context=context,
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
        )

        # Extract first 3 lines as summary
        lines = response.strip().split("\n")
        summary = "\n".join(lines[:3])

        return AgentReport(
            agent_name=self.name,
            summary=summary,
            body=response,
        )
