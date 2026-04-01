"""Shared data types used across all layers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ConnectorResult(BaseModel):
    """Standardized result returned by every connector."""

    source: str
    fetched_at: datetime = Field(default_factory=datetime.now)
    items: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def summary_line(self) -> str:
        if self.error:
            return f"[{self.source}] ERROR: {self.error}"
        return f"[{self.source}] {len(self.items)} items"


class AgentReport(BaseModel):
    """Report produced by an agent, ready for output delivery."""

    agent_name: str
    generated_at: datetime = Field(default_factory=datetime.now)
    summary: str  # Short 1-3 line summary
    body: str  # Full report text
    sources_used: list[str] = Field(default_factory=list)
    sources_failed: list[str] = Field(default_factory=list)
