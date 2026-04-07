"""APScheduler setup - registers cron jobs for all agents."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.ai.client import AIClient
from src.common.config import load_config, get_env
from src.outputs.slack_output import SlackOutput
from src.storage.database import Database

logger = logging.getLogger(__name__)


async def _init_db_and_run(agent_cls, config: dict) -> None:
    """Generic job runner: init DB, create agent, run, cleanup."""
    db = Database()
    await db.init()
    try:
        ai_client = AIClient()
        slack_output = SlackOutput(config)
        agent = agent_cls(
            config=config, ai_client=ai_client, outputs=[slack_output], db=db
        )
        await agent.run()
    finally:
        await db.close()


async def run_morning_briefing(config: dict) -> None:
    from src.agents.morning_briefing import MorningBriefingAgent
    await _init_db_and_run(MorningBriefingAgent, config)


async def run_task_monitor(config: dict) -> None:
    from src.agents.task_monitor import TaskMonitorAgent
    await _init_db_and_run(TaskMonitorAgent, config)


async def run_whatsapp_sync(config: dict) -> None:
    """Sync WhatsApp messages from bridge to PostgreSQL."""
    from src.ingestion.whatsapp_ingest import WhatsAppIngestor

    db = Database()
    await db.init()
    try:
        bridge_url = get_env("WHATSAPP_BRIDGE_URL")
        ingestor = WhatsAppIngestor(db, bridge_url)
        await ingestor.sync()
    except Exception as e:
        logger.error(f"WhatsApp sync failed: {e}")
    finally:
        await db.close()


async def run_gmail_sync(config: dict) -> None:
    """Sync Gmail emails to PostgreSQL events table."""
    from src.ingestion.gmail_ingest import GmailIngestor

    db = Database()
    await db.init()
    try:
        ingestor = GmailIngestor(db)
        await ingestor.sync(lookback_hours=1)
    except Exception as e:
        logger.error(f"Gmail sync failed: {e}")
    finally:
        await db.close()


async def run_calendar_sync(config: dict) -> None:
    """Sync Google Calendar events to PostgreSQL events table."""
    from src.ingestion.calendar_ingest import CalendarIngestor

    db = Database()
    await db.init()
    try:
        ingestor = CalendarIngestor(db, config)
        await ingestor.sync(days_ahead=2)
    except Exception as e:
        logger.error(f"Calendar sync failed: {e}")
    finally:
        await db.close()


async def run_asana_sync(config: dict) -> None:
    """Sync Asana tasks to PostgreSQL events table."""
    from src.ingestion.asana_ingest import AsanaIngestor

    db = Database()
    await db.init()
    try:
        ingestor = AsanaIngestor(db, config)
        await ingestor.sync()
    except Exception as e:
        logger.error(f"Asana sync failed: {e}")
    finally:
        await db.close()


async def run_ideaerp_metrics(config: dict) -> None:
    """Sync IdeaERP order metrics to business_metrics table."""
    from src.ingestion.ideaerp_ingest import IdeaERPMetricsIngestor

    db = Database()
    await db.init()
    try:
        ingestor = IdeaERPMetricsIngestor(db, config)
        await ingestor.sync()
    except Exception as e:
        logger.error(f"IdeaERP metrics sync failed: {e}")
    finally:
        await db.close()


def _parse_cron(cron_expr: str) -> dict:
    parts = cron_expr.split()
    return {
        "minute": parts[0],
        "hour": parts[1],
        "day": parts[2],
        "month": parts[3],
        "day_of_week": parts[4],
    }


async def create_scheduler(config: dict) -> AsyncIOScheduler:
    """Create and configure the scheduler with all agent jobs."""
    # Seed entities on startup
    from src.main import init_system
    db, resolver = await init_system(config)
    await db.close()

    agents_config = config.get("agents", {})
    timezone = agents_config.get("timezone", "Europe/Warsaw")

    scheduler = AsyncIOScheduler(timezone=timezone)

    # Morning Briefing
    briefing_config = agents_config.get("morning_briefing", {})
    briefing_cron = briefing_config.get("schedule", "0 7 * * 1-5")
    scheduler.add_job(
        run_morning_briefing,
        trigger=CronTrigger(**_parse_cron(briefing_cron), timezone=timezone),
        args=[config],
        id="morning_briefing",
        name="Morning Briefing",
        replace_existing=True,
    )
    logger.info(f"Scheduled morning_briefing: {briefing_cron} ({timezone})")

    # Task Monitor
    monitor_config = agents_config.get("task_monitor", {})
    monitor_cron = monitor_config.get("schedule", "0 9,16 * * 1-5")
    scheduler.add_job(
        run_task_monitor,
        trigger=CronTrigger(**_parse_cron(monitor_cron), timezone=timezone),
        args=[config],
        id="task_monitor",
        name="Task Monitor",
        replace_existing=True,
    )
    logger.info(f"Scheduled task_monitor: {monitor_cron} ({timezone})")

    # WhatsApp Sync - every 5 minutes
    wa_config = agents_config.get("whatsapp_sync", {})
    wa_cron = wa_config.get("schedule", "*/5 * * * *")
    scheduler.add_job(
        run_whatsapp_sync,
        trigger=CronTrigger(**_parse_cron(wa_cron), timezone=timezone),
        args=[config],
        id="whatsapp_sync",
        name="WhatsApp Sync",
        replace_existing=True,
    )
    logger.info(f"Scheduled whatsapp_sync: {wa_cron} ({timezone})")

    # Gmail Sync - every 15 minutes
    gmail_config = agents_config.get("gmail_sync", {})
    gmail_cron = gmail_config.get("schedule", "*/15 * * * *")
    scheduler.add_job(
        run_gmail_sync,
        trigger=CronTrigger(**_parse_cron(gmail_cron), timezone=timezone),
        args=[config],
        id="gmail_sync",
        name="Gmail Sync",
        replace_existing=True,
    )
    logger.info(f"Scheduled gmail_sync: {gmail_cron} ({timezone})")

    # Calendar Sync - every 30 minutes
    cal_config = agents_config.get("calendar_sync", {})
    cal_cron = cal_config.get("schedule", "*/30 * * * *")
    scheduler.add_job(
        run_calendar_sync,
        trigger=CronTrigger(**_parse_cron(cal_cron), timezone=timezone),
        args=[config],
        id="calendar_sync",
        name="Calendar Sync",
        replace_existing=True,
    )
    logger.info(f"Scheduled calendar_sync: {cal_cron} ({timezone})")

    # Asana Sync - every 15 minutes
    asana_config = agents_config.get("asana_sync", {})
    asana_cron = asana_config.get("schedule", "*/15 * * * *")
    scheduler.add_job(
        run_asana_sync,
        trigger=CronTrigger(**_parse_cron(asana_cron), timezone=timezone),
        args=[config],
        id="asana_sync",
        name="Asana Sync",
        replace_existing=True,
    )
    logger.info(f"Scheduled asana_sync: {asana_cron} ({timezone})")

    # IdeaERP Metrics - every hour
    erp_config = agents_config.get("ideaerp_metrics", {})
    erp_cron = erp_config.get("schedule", "0 * * * *")
    scheduler.add_job(
        run_ideaerp_metrics,
        trigger=CronTrigger(**_parse_cron(erp_cron), timezone=timezone),
        args=[config],
        id="ideaerp_metrics",
        name="IdeaERP Metrics",
        replace_existing=True,
    )
    logger.info(f"Scheduled ideaerp_metrics: {erp_cron} ({timezone})")

    return scheduler
