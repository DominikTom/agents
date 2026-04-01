"""Slack output - sends formatted reports to Slack channels."""

from __future__ import annotations

import logging

from slack_sdk import WebClient

from src.common.config import get_env
from src.common.types import AgentReport
from src.outputs.base import BaseOutput

logger = logging.getLogger(__name__)


class SlackOutput(BaseOutput):
    name = "slack"

    def __init__(self, config: dict):
        self.config = config
        self._client = None

    def _get_client(self) -> WebClient:
        if self._client is None:
            self._client = WebClient(token=get_env("SLACK_BOT_TOKEN"))
        return self._client

    def _get_channel(self, agent_name: str) -> str:
        """Get the Slack channel for a given agent."""
        agents_config = self.config.get("agents", {}).get(agent_name, {})
        outputs = agents_config.get("outputs", [])
        for out in outputs:
            if isinstance(out, dict) and out.get("type") == "slack":
                return out.get("channel", "#general")
        return "#general"

    async def send(self, report: AgentReport) -> None:
        """Send report to Slack channel."""
        client = self._get_client()
        channel = self._get_channel(report.agent_name)

        # Split long messages (Slack limit: 4000 chars per message)
        body = report.body
        chunks = []
        while body:
            if len(body) <= 3900:
                chunks.append(body)
                break
            # Find a good split point
            split_at = body[:3900].rfind("\n")
            if split_at == -1:
                split_at = 3900
            chunks.append(body[:split_at])
            body = body[split_at:].lstrip("\n")

        for i, chunk in enumerate(chunks):
            client.chat_postMessage(
                channel=channel,
                text=chunk,
                unfurl_links=False,
                unfurl_media=False,
            )

        # Add footer with metadata if sources failed
        if report.sources_failed:
            footer = f"⚠️ _Niedostępne źródła: {', '.join(report.sources_failed)}_"
            client.chat_postMessage(
                channel=channel,
                text=footer,
                unfurl_links=False,
            )

        logger.info(f"Slack: sent report to {channel} ({len(chunks)} message(s))")
