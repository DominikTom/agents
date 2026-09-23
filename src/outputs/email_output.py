"""E-mail delivery — Resend (same provider as MyBed OS) or plain SMTP."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

import httpx

from src.common.config import get_env_optional

logger = logging.getLogger(__name__)


def email_configured() -> str | None:
    if get_env_optional("RESEND_API_KEY"):
        return "resend"
    if get_env_optional("SMTP_HOST"):
        return "smtp"
    return None


def sender() -> str:
    return get_env_optional("EMAIL_FROM") or "MyBed Agents <onboarding@resend.dev>"


async def send_email(to: list[str], subject: str, html: str, text: str) -> str:
    """Send one message; returns the provider used. Raises on failure."""
    to = [t.strip() for t in to if t and t.strip()]
    if not to:
        raise ValueError("Brak adresatów e-maila")
    provider = email_configured()
    if provider == "resend":
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {get_env_optional('RESEND_API_KEY')}"},
                json={"from": sender(), "to": to, "subject": subject, "html": html, "text": text},
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Resend {resp.status_code}: {resp.text[:200]}")
        return "resend"
    if provider == "smtp":
        await asyncio.to_thread(_smtp_send, to, subject, html, text)
        return "smtp"
    raise RuntimeError("E-mail nie jest skonfigurowany (RESEND_API_KEY albo SMTP_HOST)")


def _smtp_send(to: list[str], subject: str, html: str, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender()
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    host = get_env_optional("SMTP_HOST")
    port = int(get_env_optional("SMTP_PORT") or 587)
    user = get_env_optional("SMTP_USER")
    password = get_env_optional("SMTP_PASSWORD")
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if user:
            smtp.login(user, password or "")
        smtp.send_message(msg)
