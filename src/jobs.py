"""Background jobs run by the scheduler (and on demand from the panel)."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from src.common.config import load_config
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
}
