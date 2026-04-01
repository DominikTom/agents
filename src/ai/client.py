"""Claude API wrapper for AI summarization."""

from __future__ import annotations

import logging

import anthropic

from src.common.config import get_env

logger = logging.getLogger(__name__)


class AIClient:
    """Thin wrapper around the Anthropic SDK."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=get_env("ANTHROPIC_API_KEY"))

    async def summarize(
        self,
        context: str,
        system_prompt: str,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
    ) -> str:
        """Send data to Claude for summarization.

        Args:
            context: The raw data/context to summarize.
            system_prompt: Instructions for how to summarize.
            model: Claude model to use.
            max_tokens: Max response tokens.

        Returns:
            The AI-generated summary text.
        """
        logger.info(f"Calling Claude ({model}), context length: {len(context)} chars")

        message = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": context}],
        )

        response_text = message.content[0].text
        logger.info(
            f"Claude response: {len(response_text)} chars, "
            f"tokens: {message.usage.input_tokens}in/{message.usage.output_tokens}out"
        )
        return response_text
