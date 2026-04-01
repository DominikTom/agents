"""Slack connector - reads recent messages from configured channels."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from slack_sdk import WebClient

from src.common.config import get_env
from src.common.types import ConnectorResult
from src.connectors.base import BaseConnector

logger = logging.getLogger(__name__)


class SlackReaderConnector(BaseConnector):
    name = "slack"

    def __init__(self, config: dict):
        super().__init__(config)
        self._client = None

    def _get_client(self) -> WebClient:
        if self._client is None:
            self._client = WebClient(token=get_env("SLACK_BOT_TOKEN"))
        return self._client

    async def fetch(self, since: datetime | None = None) -> ConnectorResult:
        try:
            client = self._get_client()
            slack_config = self.config.get("connectors", {}).get("slack", {})
            channels = slack_config.get("channels", [])
            include_dms = slack_config.get("include_dms", False)

            if since is None:
                since = datetime.now() - timedelta(hours=13)

            oldest = str(since.timestamp())
            items = []

            # Resolve channel names to IDs
            channels_response = client.conversations_list(types="public_channel,private_channel")
            channel_map = {
                f"#{ch['name']}": ch["id"]
                for ch in channels_response.get("channels", [])
            }

            for channel_name in channels:
                channel_id = channel_map.get(channel_name)
                if not channel_id:
                    logger.warning(f"Slack channel not found: {channel_name}")
                    continue

                history = client.conversations_history(
                    channel=channel_id, oldest=oldest, limit=50
                )

                for msg in history.get("messages", []):
                    if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
                        continue

                    # Get user name
                    user_id = msg.get("user", "")
                    user_name = user_id
                    if user_id:
                        try:
                            user_info = client.users_info(user=user_id)
                            user_name = user_info["user"].get("real_name", user_id)
                        except Exception:
                            pass

                    items.append({
                        "channel": channel_name,
                        "user": user_name,
                        "text": msg.get("text", ""),
                        "timestamp": msg.get("ts", ""),
                        "thread_replies": msg.get("reply_count", 0),
                    })

            if include_dms:
                dm_response = client.conversations_list(types="im", limit=20)
                for dm in dm_response.get("channels", []):
                    history = client.conversations_history(
                        channel=dm["id"], oldest=oldest, limit=10
                    )
                    for msg in history.get("messages", []):
                        user_id = msg.get("user", "")
                        user_name = user_id
                        if user_id:
                            try:
                                user_info = client.users_info(user=user_id)
                                user_name = user_info["user"].get("real_name", user_id)
                            except Exception:
                                pass

                        items.append({
                            "channel": f"DM:{user_name}",
                            "user": user_name,
                            "text": msg.get("text", ""),
                            "timestamp": msg.get("ts", ""),
                        })

            logger.info(f"Slack: fetched {len(items)} messages from {len(channels)} channels")
            return self._success_result(items)

        except Exception as e:
            logger.error(f"Slack fetch failed: {e}")
            return self._error_result(str(e))
