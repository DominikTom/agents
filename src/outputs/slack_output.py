"""Slack delivery (optional) — posts a report to a channel in 3900-char chunks."""

from __future__ import annotations

import asyncio
import logging

from slack_sdk import WebClient

from src.common.config import get_env

logger = logging.getLogger(__name__)


def _chunks(text: str, size: int = 3900) -> list[str]:
    out = []
    while text:
        if len(text) <= size:
            out.append(text)
            break
        cut = text[:size].rfind("\n")
        cut = cut if cut > 0 else size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


async def post_text(channel: str, text: str) -> None:
    client = WebClient(token=get_env("SLACK_BOT_TOKEN"))
    for chunk in _chunks(text):
        await asyncio.to_thread(
            client.chat_postMessage, channel=channel, text=chunk, unfurl_links=False, unfurl_media=False
        )
    logger.info(f"Slack: posted to {channel}")
