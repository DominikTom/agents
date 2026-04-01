"""Base output interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.common.types import AgentReport


class BaseOutput(ABC):
    """Abstract base for all output adapters."""

    name: str = "base"

    @abstractmethod
    async def send(self, report: AgentReport) -> None:
        """Deliver a report to the destination."""
        ...
