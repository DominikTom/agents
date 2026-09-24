"""Background jobs run by the scheduler (and on demand from the panel)."""

from __future__ import annotations

import html
import logging
import time
from contextlib import asynccontextmanager

from src.common.config import get_env_optional, load_config
from src.common.timeutil import now, today
from src.storage.database import Database

logger = logging.getLogger(__name__)


@asynccontextmanager
async def database():
    db = Database()
    await db.init(run_schema=False)
    try:
        yield db
    finally:
        await db.close()


async def _logged(db: Database, name: str, coro) -> dict | None:
    started = time.monotonic()
    try:
        result = await coro
        err = result.get("error") if isinstance(result, dict) else None
        await db.log_run(name, "error" if err else "success", err, int((time.monotonic() - started) * 1000))
        return result
    except Exception as e:
        logger.exception(f"Job {name} failed")
        await db.log_run(name, "error", str(e)[:500], int((time.monotonic() - started) * 1000))
        return None


async def whatsapp_sync(db: Database | None = None) -> dict | None:
    from src.ingestion.whatsapp_ingest import WhatsAppIngestor

    if db is None:
        async with database() as db:
            return await whatsapp_sync(db)
    return await _logged(db, "whatsapp_sync", WhatsAppIngestor(db).sync())


async def gmail_sync(db: Database | None = None) -> dict | None:
    from src.ingestion.gmail_ingest import GmailIngestor

    if db is None:
        async with database() as db:
            return await gmail_sync(db)
    # Catch up after downtime: look back to the last ingest (max 3 days)
    last = await db.last_ingest_at("gmail")
    hours = 1
    if last:
        hours = min(72, max(1, int((now() - last).total_seconds() // 3600) + 1))
    return await _logged(db, "gmail_sync", GmailIngestor(db).sync(lookback_hours=hours))


async def calendar_sync(db: Database | None = None) -> dict | None:
    from src.ingestion.calendar_ingest import CalendarIngestor

    if db is None:
        async with database() as db:
            return await calendar_sync(db)
    return await _logged(db, "calendar_sync", CalendarIngestor(db, load_config()).sync(days_ahead=10))


async def ideaerp_sync(db: Database | None = None) -> dict | None:
    from src.ingestion.ideaerp_ingest import IdeaERPMetricsIngestor

    if db is None:
        async with database() as db:
            return await ideaerp_sync(db)
    return await _logged(db, "ideaerp_sync", IdeaERPMetricsIngestor(db, load_config()).sync())


async def os_people_sync(db: Database | None = None) -> dict | None:
    from src.ingestion.os_people_sync import sync_os_people

    if db is None:
        async with database() as db:
            return await os_people_sync(db)
    return await _logged(db, "os_people_sync", sync_os_people(db))


async def chat_digests(db: Database | None = None, day=None, force: bool = False) -> dict | None:
    from src.ai.client import AIClient
    from src.processing.chat_digest import ChatDigester

    if db is None:
        async with database() as db:
            return await chat_digests(db, day, force)
    return await _logged(db, "chat_digests", ChatDigester(db, AIClient(db=db)).run(day or today(), force=force))


async def topic_extraction(db: Database | None = None) -> dict | None:
    from src.ai.client import AIClient
    from src.ingestion.topic_extractor import TopicExtractor

    if db is None:
        async with database() as db:
            return await topic_extraction(db)
    await db.close_stale_topics(days=14)
    return await _logged(db, "topic_extraction", TopicExtractor(db, AIClient(db=db)).extract(lookback_hours=24))


WA_ALERT_AFTER_MIN = 15


async def whatsapp_watchdog(db: Database | None = None) -> dict | None:
    """Alert (Slack + e-mail) when WhatsApp has been disconnected for 15+ min, and when it's back."""
    from src.connectors.whatsapp_bridge import BridgeClient, BridgeError

    if db is None:
        async with database() as db:
            return await whatsapp_watchdog(db)
    try:
        status = (await BridgeClient(timeout=10).health()).get("status") or "?"
    except BridgeError:
        status = "bridge_down"
    st = await db.get_setting("wa_watchdog", {}) or {}
    ts = time.time()
    if status == "connected":
        if st.get("alerted"):
            await _notify("✅ WhatsApp znowu połączony", "Agenci znów czytają WhatsApp. Luka zostanie uzupełniona automatycznie.")
        if st:
            await db.set_setting("wa_watchdog", {})
        return {"status": status}
    before = dict(st)
    if not st.get("down_since"):
        st = {"down_since": ts, "alerted": False}
    minutes = int((ts - st["down_since"]) // 60)
    if minutes >= WA_ALERT_AFTER_MIN and not st.get("alerted"):
        base = (get_env_optional("PUBLIC_BASE_URL") or "").rstrip("/")
        link = f"{base}/sources/whatsapp" if base else "panel → Źródła danych → WhatsApp"
        await _notify(
            "⚠️ WhatsApp rozłączony",
            f"Od {minutes} min agenci nie czytają WhatsAppa (stan: {status}). "
            f"Raporty nie zobaczą nowych rozmów. Połącz ponownie: {link}",
        )
        st["alerted"] = True
    if st != before:
        await db.set_setting("wa_watchdog", st)
    return {"status": status, "down_min": minutes}


async def _notify(title: str, text: str) -> None:
    """Short system alert: Slack channel of the morning briefing (if set) + e-mail recipients."""
    from src.outputs.email_output import email_configured, send_email
    from src.reports.profiles import load_general, load_profiles

    async with database() as db:
        general = await load_general(db)
        profiles = await load_profiles(db)
    channel = next((p.get("slack_channel") for p in profiles.values() if (p.get("slack_channel") or "").strip()), None)
    if channel and get_env_optional("SLACK_BOT_TOKEN"):
        try:
            from src.outputs.slack_output import post_text

            await post_text(channel.strip(), f"*{title}*\n{text}")
        except Exception as e:
            logger.warning(f"Alert to Slack failed: {e}")
    recipients = general.get("recipients") or []
    if email_configured() and recipients:
        try:
            await send_email(recipients, title, f"<p>{html.escape(text)}</p>", text)
        except Exception as e:
            logger.warning(f"Alert e-mail failed: {e}")


async def run_report(key: str, db: Database | None = None, deliver: bool = True) -> dict | None:
    from src.reports.engine import ReportEngine

    if db is None:
        async with database() as db:
            return await run_report(key, db, deliver)
    try:
        return await ReportEngine(db).run(key, deliver=deliver)
    except Exception:
        return None  # already logged by the engine


# name -> (label, coroutine factory) — used by the scheduler and the "Uruchom" buttons
JOBS = {
    "whatsapp_sync": ("Synchronizacja WhatsApp", whatsapp_sync),
    "gmail_sync": ("Synchronizacja Gmail", gmail_sync),
    "calendar_sync": ("Synchronizacja kalendarza", calendar_sync),
    "ideaerp_sync": ("Metryki IdeaERP", ideaerp_sync),
    "os_people_sync": ("Ludzie z MyBed OS", os_people_sync),
    "chat_digests": ("Streszczenia czatów", chat_digests),
    "topic_extraction": ("Wątki przekrojowe", topic_extraction),
    "whatsapp_watchdog": ("Alarm: WhatsApp rozłączony", whatsapp_watchdog),
}
