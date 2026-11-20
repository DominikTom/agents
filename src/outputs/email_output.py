"""E-mail delivery — Resend (same provider as MyBed OS) or plain SMTP."""

from __future__ import annotations

import asyncio
import base64
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


def _clean(addrs: list[str] | None) -> list[str]:
    return [a.strip() for a in addrs or [] if a and a.strip()]


async def send_email(to: list[str], subject: str, html: str, text: str, *, cc: list[str] | None = None,
                     reply_to: list[str] | None = None, attachments: list[dict] | None = None) -> str:
    """Send one message; returns the provider used. Raises on failure.
    attachments: [{"filename": "x.csv", "content": bytes, "content_type": "text/csv"}]."""
    to, reply_to = _clean(to), _clean(reply_to)
    cc = [a for a in _clean(cc) if a.lower() not in {t.lower() for t in to}]
    if not to:
        raise ValueError("Brak adresatów e-maila")
    attachments = attachments or []
    provider = email_configured()
    if provider == "resend":
        payload: dict = {"from": sender(), "to": to, "subject": subject, "html": html, "text": text}
        if cc:
            payload["cc"] = cc
        if reply_to:
            payload["reply_to"] = reply_to
        if attachments:
            payload["attachments"] = [
                {"filename": a["filename"], "content": base64.b64encode(a["content"]).decode("ascii"),
                 **({"content_type": a["content_type"]} if a.get("content_type") else {})}
                for a in attachments
            ]
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {get_env_optional('RESEND_API_KEY')}"},
                json=payload,
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Resend {resp.status_code}: {resp.text[:200]}")
        return "resend"
    if provider == "smtp":
        await asyncio.to_thread(_smtp_send, to, subject, html, text, cc, reply_to, attachments)
        return "smtp"
    raise RuntimeError("E-mail nie jest skonfigurowany (RESEND_API_KEY albo SMTP_HOST)")


def build_message(to: list[str], subject: str, html: str, text: str, cc: list[str] | None = None,
                  reply_to: list[str] | None = None, attachments: list[dict] | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to:
        msg["Reply-To"] = ", ".join(reply_to)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for a in attachments or []:
        maintype, _, subtype = (a.get("content_type") or "application/octet-stream").partition("/")
        msg.add_attachment(a["content"], maintype=maintype, subtype=subtype.split(";")[0] or "octet-stream",
                           filename=a["filename"])
    return msg


def _smtp_send(to: list[str], subject: str, html: str, text: str, cc: list[str] | None = None,
               reply_to: list[str] | None = None, attachments: list[dict] | None = None) -> None:
    msg = build_message(to, subject, html, text, cc, reply_to, attachments)
    host = get_env_optional("SMTP_HOST")
    port = int(get_env_optional("SMTP_PORT") or 587)
    user = get_env_optional("SMTP_USER")
    password = get_env_optional("SMTP_PASSWORD")
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if user:
            smtp.login(user, password or "")
        smtp.send_message(msg)  # To + Cc headers are the envelope recipients
