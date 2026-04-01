"""Test configuration and shared fixtures."""

import pytest


@pytest.fixture
def sample_config():
    """Basic config for testing."""
    return {
        "agents": {
            "timezone": "Europe/Warsaw",
            "morning_briefing": {
                "schedule": "0 7 * * 1-5",
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4096,
                "email_lookback_hours": 13,
                "connectors": ["gmail", "slack", "asana"],
                "outputs": [{"type": "slack", "channel": "#test"}],
            },
            "task_monitor": {
                "schedule": "0 9,16 * * 1-5",
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 2048,
                "blocked_threshold_days": 3,
                "velocity_window_weeks": 4,
                "connectors": ["asana"],
                "outputs": [{"type": "slack", "channel": "#test"}],
            },
        },
        "connectors": {
            "gmail": {"categories": {"urgent": {"keywords": ["pilne"]}}},
            "slack": {"channels": ["#general"]},
            "asana": {"projects": []},
        },
    }
