"""Base connector interface. All connectors inherit from this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from src.common.types import ConnectorResult


class BaseConnector(ABC):
    """Abstract base for all data source connectors.

    Every connector is read-only and returns a ConnectorResult.
    """

    name: str = "base"

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    async def fetch(self, since: datetime | None = None) -> ConnectorResult:
        """Fetch data from the source.

        Args:
            since: Only fetch data newer than this timestamp.
                   If None, uses connector-specific defaults.
        """
        ...

    def _error_result(self, error: str) -> ConnectorResult:
        """Helper to create an error result."""
        return ConnectorResult(source=self.name, error=error)

    def _success_result(self, items: list[dict]) -> ConnectorResult:
        """Helper to create a success result."""
        return ConnectorResult(source=self.name, items=items)
