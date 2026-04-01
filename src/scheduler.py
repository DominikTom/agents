"""APScheduler setup - registers cron jobs for all agents."""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.ai.client import AIClient
from src.common.config import load_config
from src.outputs.slack_output import SlackOutput
from src.storage.database import Database

logger = logging.getLogger(__name__)


async def run_morning_briefing(config: dict) -> None:
    """Job function for morning briefing."""
    from src.agents.morning_briefing import MorningBriefingAgent

    db = Database()
    await db.init()
    try:
        ai_client = AIClient()
        slack_output = SlackOutput(config)
        agent = MorningBriefingAgent(
            config=config, ai_client=ai_client, outputs=[slack_output], db=db
        )
        await agent.run()
    finally:
        await db.close()


async def run_task_monitor(config: dict) -> None:
    """Job function for task monitor."""
    from src.agents.task_monitor import TaskMonitorAgent

    db = Database()
    await db.init()
    try:
        ai_client = AIClient()
        slack_output = SlackOutput(config)
        agent = TaskMonitorAgent(
            config=config, ai_client=ai_client, outputs=[slack_output], db=db
        )
        await agent.run()
    finally:
        await db.close()


def _parse_cron(cron_expr: str) -> dict:
    """Parse '0 7 * * 1-5' into CronTrigger kwargs."""
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

    return scheduler
