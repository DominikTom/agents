"""Configuration loader - merges .env secrets with YAML config files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Project root = parent of src/
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def load_config() -> dict[str, Any]:
    """Load all configuration: .env + YAML files."""
    load_dotenv(PROJECT_ROOT / ".env")

    config: dict[str, Any] = {}

    for yaml_file in ["agents.yaml", "connectors.yaml", "people.yaml"]:
        path = CONFIG_DIR / yaml_file
        if path.exists():
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            key = yaml_file.removesuffix(".yaml")
            config[key] = data

    return config


def get_env(key: str, default: str | None = None) -> str:
    """Get an environment variable, raise if required and missing."""
    value = os.getenv(key, default)
    if value is None:
        raise EnvironmentError(f"Missing required environment variable: {key}")
    return value


def get_env_optional(key: str) -> str | None:
    """Get an optional environment variable."""
    return os.getenv(key)
