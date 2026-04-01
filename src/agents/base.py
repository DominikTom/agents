"""Base agent interface. All agents inherit from this."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime

from src.ai.client import AIClient
from src.common.types import AgentReport, ConnectorResult
from src.outputs.base import BaseOutput
from src.storage.database import Database

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Abstract base for all agents.

    Flow: gather_data() -> analyze() -> deliver()
    """

    name: str = "base"

    def __init__(
        self,
        config: dict,
        ai_client: AIClient,
        outputs: list[BaseOutput],
        db: Database,
    ):
        self.config = config
        self.ai = ai_client
        self.outputs = outputs
        self.db = db

    async def run(self) -> AgentReport | None:
        """Main orchestration: gather -> analyze -> deliver."""
        start = datetime.now()

        try:
            data = await self.gather_data()

            sources_ok = [k for k, v in data.items() if v.ok]
            sources_failed = [k for k, v in data.items() if not v.ok]

            if not sources_ok:
                logger.error(f"[{self.name}] All connectors failed, skipping.")
                await self.db.log_run(self.name, "error", "All connectors failed")
                return None

            for name, result in data.items():
                if not result.ok:
                    logger.warning(f"[{self.name}] Connector {name} failed: {result.error}")

            report = await self.analyze(data)
            report.sources_used = sources_ok
            report.sources_failed = sources_failed

            await self.deliver(report)

            duration_ms = int((datetime.now() - start).total_seconds() * 1000)
            await self.db.log_run(self.name, "success", duration_ms=duration_ms)
            logger.info(f"[{self.name}] Completed in {duration_ms}ms")

            return report

        except Exception as e:
            logger.exception(f"[{self.name}] Failed")
            await self.db.log_run(self.name, "error", str(e))
            return None

    @abstractmethod
    async def gather_data(self) -> dict[str, ConnectorResult]:
        """Fetch data from all relevant connectors."""
        ...

    @abstractmethod
    async def analyze(self, data: dict[str, ConnectorResult]) -> AgentReport:
        """Process gathered data with AI and produce a report."""
        ...

    async def deliver(self, report: AgentReport) -> None:
        """Send the report through all configured outputs."""
        for output in self.outputs:
            try:
                await output.send(report)
            except Exception as e:
                logger.error(f"[{self.name}] Output {output.name} failed: {e}")
