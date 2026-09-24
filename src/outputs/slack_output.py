"""Slack delivery (optional) — posts a report to a channel in 3900-char chunks."""

from __future__ import annotations

import asyncio
import logging
import re

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


def md_to_slack(md: str) -> str:
    """Markdown from the report model → Slack mrkdwn (headings, bold, links, tables)."""
    out: list[str] = []
    table: list[str] = []

    def flush_table():
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|?[\s:|-]+\|?", r)]
            cells = [[c.strip().replace("**", "") for c in r.strip().strip("|").split("|")] for r in rows]
            widths = [max(len(row[i]) if i < len(row) else 0 for row in cells) for i in range(max(map(len, cells)))]
            lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip() for row in cells]
            out.append("```\n" + "\n".join(lines) + "\n```")
            table.clear()

    for line in md.splitlines():
        if line.lstrip().startswith("|"):
            table.append(line)
            continue
        flush_table()
        m = re.match(r"^#{1,6}\s+(.*)", line)
        if m:
            line = f"*{m.group(1).strip()}*"
        else:
            line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
            line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", line)
        out.append(line)
    flush_table()
    return "\n".join(out)


async def post_text(channel: str, text: str) -> None:
    client = WebClient(token=get_env("SLACK_BOT_TOKEN"))
    for chunk in _chunks(text):
        await asyncio.to_thread(
            client.chat_postMessage, channel=channel, text=chunk, unfurl_links=False, unfurl_media=False
        )
    logger.info(f"Slack: posted to {channel}")
