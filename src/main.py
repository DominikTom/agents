"""Entry point.

Usage:
    python -m src.main schedule                 # scheduler daemon (ingestion + reports)
    python -m src.main report morning_briefing  # generate + deliver a report now
    python -m src.main report daily_wrap --no-deliver
    python -m src.main job whatsapp_sync        # run one background job now
    python -m src.main test-connector gmail
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from src.common.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("agents")

REPORT_ALIASES = {"briefing": "morning_briefing", "wrap": "daily_wrap", "weekly": "weekly_review"}


async def init_system(config: dict):
    """Create schema, seed entities from people.yaml."""
    from src.processing.entity_resolver import EntityResolver
    from src.storage.database import Database

    db = Database()
    await db.init()
    await EntityResolver(db).seed_from_config(config.get("people", {}))
    return db


async def start_scheduler(config: dict) -> None:
    from src import jobs
    from src.scheduler import AgentsScheduler

    db = await init_system(config)
    await db.close()
    scheduler = AgentsScheduler(config)
    scheduler.start()
    await scheduler.reload_reports()
    await jobs.os_people_sync()
    logger.info("Scheduler started.")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.scheduler.shutdown()


async def run_report(key: str, deliver: bool) -> None:
    from src import jobs

    result = await jobs.run_report(REPORT_ALIASES.get(key, key), deliver=deliver)
    if not result:
        sys.exit(1)
    print(result["title"])
    print(result["lead"])
    print(result["markdown"])


async def run_job(name: str) -> None:
    from src import jobs

    if name not in jobs.JOBS:
        logger.error(f"Unknown job: {name}. Available: {', '.join(jobs.JOBS)}")
        sys.exit(1)
    print(await jobs.JOBS[name][1]())


async def test_connector(name: str, config: dict) -> None:
    from src.connectors import create_connector

    connector = create_connector(name, config)
    if connector is None:
        logger.error(f"Unknown connector: {name}")
        sys.exit(1)
    result = await connector.fetch()
    print(result.summary_line)
    for item in result.items[:3]:
        print("  ", item)


async def erp_check(day: str | None, config: dict) -> None:
    """Sales of one day per shop from IdeaERP and from the dash warehouse, side by side (no order data printed)."""
    from datetime import date as _date, timedelta as _td

    from src.common.timeutil import today
    from src.connectors.dash import DashClient
    from src.connectors.ideaerp import erp_daily_sales

    d = _date.fromisoformat(day) if day else today() - _td(days=1)
    dash = DashClient()
    dash_rows = await dash.revenue(d - _td(days=14), d) if dash.configured else []
    rates = [float(r["revenue_gross_pln"]) / float(r["revenue_gross_original"]) for r in dash_rows
             if r.get("original_currency") == "EUR" and float(r.get("revenue_gross_original") or 0)]
    erp = await erp_daily_sales(d, config, sum(rates) / len(rates) if rates else 4.25)
    print(f"Dzień {d} · sklepy w ERP: {erp['shops']} · pole z numerem zamówienia: {erp['ref_key']}")
    by_erp = {r["source_shop"]: r for r in erp["rows"]}
    by_dash = {r["source_shop"]: r for r in dash_rows if r.get("date") == d.isoformat()}
    print(f"{'sklep':<16}{'ERP zam.':>10}{'ERP zł':>14}{'dash zam.':>11}{'dash zł':>14}")
    for shop in sorted(set(by_erp) | set(by_dash)):
        e, w = by_erp.get(shop, {}), by_dash.get(shop, {})
        print(f"{shop:<16}{e.get('orders_count', '—'):>10}{round(e['revenue_gross_pln']) if e else '—':>14}"
              f"{w.get('orders_count', '—'):>11}{round(float(w['revenue_gross_pln'])) if w else '—':>14}")


def main() -> None:
    parser = argparse.ArgumentParser(description="MyBed Agents")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schedule", help="Start scheduler daemon")
    rep = sub.add_parser("report", help="Generate a report now")
    rep.add_argument("key", help="morning_briefing | daily_wrap | weekly_review")
    rep.add_argument("--no-deliver", action="store_true", help="Save only, don't e-mail")
    job = sub.add_parser("job", help="Run a background job now")
    job.add_argument("name")
    run = sub.add_parser("run", help="(legacy) run a report: briefing | wrap | weekly")
    run.add_argument("agent")
    test = sub.add_parser("test-connector", help="Test a live connector")
    test.add_argument("connector")
    erp = sub.add_parser("erp-check", help="Compare a day's sales: IdeaERP vs dash (per shop)")
    erp.add_argument("day", nargs="?", help="YYYY-MM-DD, default yesterday")

    args = parser.parse_args()
    config = load_config()
    if args.command == "schedule":
        asyncio.run(start_scheduler(config))
    elif args.command == "report":
        asyncio.run(run_report(args.key, deliver=not args.no_deliver))
    elif args.command == "run":
        asyncio.run(run_report(args.agent, deliver=True))
    elif args.command == "job":
        asyncio.run(run_job(args.name))
    elif args.command == "test-connector":
        asyncio.run(test_connector(args.connector, config))
    elif args.command == "erp-check":
        asyncio.run(erp_check(args.day, config))


if __name__ == "__main__":
    main()
