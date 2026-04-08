"""Entry point for the agents system.

Usage:
    python -m src.main schedule          # Start scheduler daemon
    python -m src.main run briefing      # Run morning briefing now
    python -m src.main run monitor       # Run task monitor now
    python -m src.main test-connector gmail  # Test a single connector
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


async def init_system(config: dict) -> tuple:
    """Initialize database, entity resolver, and seed entities."""
    from src.storage.database import Database
    from src.processing.entity_resolver import EntityResolver

    db = Database()
    await db.init()

    # Seed entities from people.yaml
    resolver = EntityResolver(db)
    people_config = config.get("people", {})
    await resolver.seed_from_config(people_config)

    return db, resolver


async def run_agent(agent_name: str, config: dict) -> None:
    """Run a single agent immediately."""
    from src.agents.morning_briefing import MorningBriefingAgent
    from src.agents.task_monitor import TaskMonitorAgent
    from src.ai.client import AIClient
    from src.outputs.slack_output import SlackOutput
    from src.storage.database import Database
    from src.processing.entity_resolver import EntityResolver

    db = Database()
    await db.init(run_schema=False)
    resolver = EntityResolver(db)
    people_config = config.get("people", {})
    await resolver.seed_from_config(people_config)
    ai_client = AIClient()
    slack_output = SlackOutput(config)

    if agent_name in ("briefing", "morning-briefing"):
        agent = MorningBriefingAgent(
            config=config,
            ai_client=ai_client,
            outputs=[slack_output],
            db=db,
        )
    elif agent_name in ("monitor", "task-monitor"):
        agent = TaskMonitorAgent(
            config=config,
            ai_client=ai_client,
            outputs=[slack_output],
            db=db,
        )
    else:
        logger.error(f"Unknown agent: {agent_name}")
        sys.exit(1)

    logger.info(f"Running agent: {agent_name}")
    await agent.run()
    logger.info(f"Agent {agent_name} finished")


async def start_scheduler(config: dict) -> None:
    """Start the APScheduler daemon."""
    from src.scheduler import create_scheduler

    scheduler = await create_scheduler(config)
    scheduler.start()
    logger.info("Scheduler started. Press Ctrl+C to exit.")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Scheduler stopped.")


async def test_connector(connector_name: str, config: dict) -> None:
    """Test a single connector by fetching sample data."""
    from src.connectors import create_connector

    connector = create_connector(connector_name, config)
    if connector is None:
        logger.error(f"Unknown connector: {connector_name}")
        sys.exit(1)

    logger.info(f"Testing connector: {connector_name}")
    result = await connector.fetch()
    if result.ok:
        logger.info(f"Success: {result.summary_line}")
        for item in result.items[:3]:
            logger.info(f"  Sample: {item}")
    else:
        logger.error(f"Failed: {result.error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily Agents System")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("schedule", help="Start scheduler daemon")

    run_parser = subparsers.add_parser("run", help="Run an agent now")
    run_parser.add_argument(
        "agent", choices=["briefing", "monitor"], help="Agent to run"
    )

    test_parser = subparsers.add_parser("test-connector", help="Test a connector")
    test_parser.add_argument("connector", help="Connector name (gmail, asana, etc.)")

    args = parser.parse_args()
    config = load_config()

    if args.command == "schedule":
        asyncio.run(start_scheduler(config))
    elif args.command == "run":
        asyncio.run(run_agent(args.agent, config))
    elif args.command == "test-connector":
        asyncio.run(test_connector(args.connector, config))


if __name__ == "__main__":
    main()
