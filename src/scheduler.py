"""APScheduler setup — ingestion jobs + report schedules from the Studio settings."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src import jobs
from src.storage.database import Database

logger = logging.getLogger(__name__)

TZ = "Europe/Warsaw"

# job -> cron (minute hour day month dow); overridable in config/agents.yaml
DEFAULT_CRONS = {
    "whatsapp_sync": "*/5 * * * *",
    "whatsapp_watchdog": "*/5 * * * *",
    "gmail_sync": "*/10 * * * *",
    "calendar_sync": "*/30 * * * *",
    "ideaerp_sync": "5 * * * *",
    "os_people_sync": "15 */6 * * *",
    "chat_digests": "20 8-20 * * *",
    "topic_extraction": "40 15 * * mon-fri",
}


def _cron(expr: str) -> CronTrigger:
    minute, hour, day, month, dow = expr.split()
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow, timezone=TZ)


def _dow(days: list[int]) -> str:
    return ",".join(str(d) for d in sorted(set(days))) or "0-6"


class AgentsScheduler:
    def __init__(self, config: dict):
        self.config = config
        self.scheduler = AsyncIOScheduler(timezone=TZ)
        self._fingerprint = None

    def start(self) -> None:
        overrides = self.config.get("agents", {}).get("jobs", {})
        for name, (label, fn) in jobs.JOBS.items():
            expr = overrides.get(name, DEFAULT_CRONS[name])
            self.scheduler.add_job(fn, trigger=_cron(expr), id=name, name=label, replace_existing=True,
                                   max_instances=1, coalesce=True, misfire_grace_time=300)
            logger.info(f"Scheduled {name}: {expr}")
        self.scheduler.add_job(self.reload_reports, "interval", minutes=1, id="_reload_reports",
                               max_instances=1, coalesce=True)
        self.scheduler.start()

    async def reload_reports(self) -> None:
        """(Re)register report jobs when their settings change in the panel."""
        from src.reports.profiles import load_profiles

        db = Database()
        await db.init(run_schema=False)
        try:
            fp = await db.settings_fingerprint()
            if fp == self._fingerprint:
                return
            profiles = await load_profiles(db)
        finally:
            await db.close()
        self._fingerprint = fp
        for key, p in profiles.items():
            job_id = f"report:{key}"
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
            if not p.get("enabled") or not p.get("days"):
                logger.info(f"Report {key}: disabled")
                continue
            hour, minute = (p.get("time") or "07:00").split(":")
            trigger = CronTrigger(hour=int(hour), minute=int(minute), day_of_week=_dow(p["days"]), timezone=TZ)
            self.scheduler.add_job(jobs.run_report, trigger=trigger, args=[key], id=job_id, name=p.get("name", key),
                                   max_instances=1, coalesce=True, misfire_grace_time=1800)
            logger.info(f"Report {key}: {p.get('time')} dow={_dow(p['days'])}")
