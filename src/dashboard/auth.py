"""Simple authentication for the dashboard."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from src.common.config import get_env_optional

# Session tokens store: token -> expiry
_sessions: dict[str, datetime] = {}

SESSION_DURATION_HOURS = 24


def get_credentials() -> tuple[str, str]:
    """Get dashboard credentials from env."""
    username = get_env_optional("DASHBOARD_USERNAME") or "admin"
    password = get_env_optional("DASHBOARD_PASSWORD") or "admin"
    return username, password


def verify_login(username: str, password: str) -> str | None:
    """Verify credentials and return session token if valid."""
    expected_user, expected_pass = get_credentials()
    if username == expected_user and password == expected_pass:
        token = secrets.token_urlsafe(32)
        _sessions[token] = datetime.now() + timedelta(hours=SESSION_DURATION_HOURS)
        return token
    return None


def verify_session(token: str | None) -> bool:
    """Check if a session token is valid."""
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None:
        return False
    if datetime.now() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def logout(token: str) -> None:
    """Invalidate a session token."""
    _sessions.pop(token, None)
