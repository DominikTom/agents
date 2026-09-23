"""Dashboard authentication — single admin account, signed session cookies.

Sessions are HMAC-signed (they survive restarts and work across processes)
and expire after SESSION_DAYS. There is no default password: without
DASHBOARD_PASSWORD the panel refuses every login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from src.common.config import get_env_optional

SESSION_DAYS = 30
COOKIE = "session"


def _secret() -> bytes:
    explicit = get_env_optional("SESSION_SECRET")
    if explicit:
        return explicit.encode()
    # Derived from the password: changing the password logs everyone out
    seed = (get_env_optional("DASHBOARD_PASSWORD") or "") + "|agents-session-v1"
    return hashlib.sha256(seed.encode()).digest()


def verify_login(username: str, password: str) -> str | None:
    expected_user = get_env_optional("DASHBOARD_USERNAME") or "admin"
    expected_pass = get_env_optional("DASHBOARD_PASSWORD") or ""
    if not expected_pass:
        return None
    ok_user = hmac.compare_digest(username.encode(), expected_user.encode())
    ok_pass = hmac.compare_digest(password.encode(), expected_pass.encode())
    if not (ok_user and ok_pass):
        return None
    return make_token(username)


def make_token(username: str) -> str:
    expires = int(time.time()) + SESSION_DAYS * 86400
    payload = f"{username}:{expires}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def verify_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, expires, sig = raw.rsplit(":", 2)
    except Exception:
        return False
    expected = hmac.new(_secret(), f"{username}:{expires}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    return int(expires) > time.time()


def password_configured() -> bool:
    return bool(get_env_optional("DASHBOARD_PASSWORD"))
