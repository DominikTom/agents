"""Shared test setup."""

import os

os.environ.setdefault("DASHBOARD_PASSWORD", "test-password")
os.environ.setdefault("POSTGRES_PASSWORD", "unused")
