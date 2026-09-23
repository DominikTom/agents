"""Claude API wrapper: long-form report writing and structured JSON extraction."""

from __future__ import annotations

import json
import logging
import time

import anthropic

from src.common.config import get_env

logger = logging.getLogger(__name__)

# Defaults — overridable per report in the Studio (settings) and via env
REPORT_MODEL = "claude-opus-5"
EXTRACT_MODEL = "claude-sonnet-5"

MODEL_CHOICES = [
    {"id": "claude-opus-5", "label": "Claude Opus 5", "hint": "domyślny — najlepsza jakość analizy"},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "hint": "tańszy i szybszy"},
    {"id": "claude-fable-5-1", "label": "Claude Fable 5.1", "hint": "najmocniejszy, najdroższy"},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "hint": "najtańszy, do krótkich raportów"},
]

# Models with safety classifiers — opt into server-side refusal fallbacks
_FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Legacy IDs from older configs → current generation
_MODEL_ALIASES = {
    "claude-sonnet-4-20250514": "claude-sonnet-5",
    "claude-sonnet-4-5": "claude-sonnet-5",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
}


class AIRefusal(RuntimeError):
    """Claude declined the request (stop_reason == 'refusal')."""


def resolve_model(model: str | None, default: str) -> str:
    model = (model or "").strip() or default
    return _MODEL_ALIASES.get(model, model)


class AIClient:
    """Async Anthropic client with usage logging to the ai_calls table."""

    def __init__(self, db=None):
        self.client = anthropic.AsyncAnthropic(
            api_key=get_env("ANTHROPIC_API_KEY"), max_retries=3, timeout=900.0,
        )
        self.db = db

    async def write(
        self,
        *,
        system: str,
        content: str,
        model: str | None = None,
        max_tokens: int = 16000,
        effort: str | None = None,
        purpose: str = "report",
    ) -> str:
        """Generate long-form text (reports). Streams to avoid HTTP timeouts."""
        model = resolve_model(model, REPORT_MODEL)
        params: dict = {
            "model": model,
            "max_tokens": max_tokens,
            # Stable instructions first so repeated runs hit the prompt cache
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
        }
        if effort:
            params["output_config"] = {"effort": effort}

        started = time.monotonic()
        logger.info(f"Claude write ({model}, {purpose}): {len(content)} chars of context")
        try:
            if model in _FALLBACK_MODELS:
                async with self.client.beta.messages.stream(
                    **params, betas=[_FALLBACK_BETA], fallbacks="default",
                ) as stream:
                    message = await stream.get_final_message()
            else:
                async with self.client.messages.stream(**params) as stream:
                    message = await stream.get_final_message()
        except anthropic.APIError:
            await self._log(purpose, model, None, started, ok=False)
            raise

        await self._log(purpose, model, message, started, ok=message.stop_reason != "refusal")
        if message.stop_reason == "refusal":
            raise AIRefusal(f"Claude odmówił wygenerowania ({purpose})")
        text = "".join(b.text for b in message.content if b.type == "text").strip()
        if message.stop_reason == "max_tokens":
            logger.warning(f"Claude output truncated at max_tokens ({purpose})")
        return text

    async def extract(
        self,
        *,
        system: str,
        content: str,
        schema: dict,
        model: str | None = None,
        max_tokens: int = 8000,
        effort: str = "low",
        purpose: str = "extract",
    ) -> dict:
        """Structured extraction — the response is guaranteed to match `schema`."""
        model = resolve_model(model, EXTRACT_MODEL)
        started = time.monotonic()
        try:
            message = await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.APIError:
            await self._log(purpose, model, None, started, ok=False)
            raise
        await self._log(purpose, model, message, started, ok=message.stop_reason == "end_turn")
        if message.stop_reason == "refusal":
            raise AIRefusal(f"Claude odmówił analizy ({purpose})")
        text = next((b.text for b in message.content if b.type == "text"), "")
        return json.loads(text)

    async def summarize(self, context: str, system_prompt: str, model: str | None = None, max_tokens: int = 4096) -> str:
        """Backwards-compatible alias used by older code paths."""
        return await self.write(system=system_prompt, content=context, model=model, max_tokens=max_tokens)

    async def _log(self, purpose: str, model: str, message, started: float, ok: bool) -> None:
        if self.db is None:
            return
        usage = getattr(message, "usage", None)
        try:
            await self.db.log_ai_call(
                purpose=purpose,
                model=getattr(message, "model", None) or model,
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                duration_ms=int((time.monotonic() - started) * 1000),
                ok=ok,
            )
        except Exception as e:  # usage logging must never break a report
            logger.debug(f"ai_calls log failed: {e}")
