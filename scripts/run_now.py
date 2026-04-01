#!/usr/bin/env python3
"""Quick manual trigger for agents.

Usage:
    python scripts/run_now.py briefing
    python scripts/run_now.py monitor
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.main import run_agent
from src.common.config import load_config


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/run_now.py [briefing|monitor]")
        sys.exit(1)

    agent = sys.argv[1]
    config = load_config()
    asyncio.run(run_agent(agent, config))


if __name__ == "__main__":
    main()
