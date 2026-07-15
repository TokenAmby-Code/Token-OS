#!/usr/bin/env python3
"""Standalone wrapper for the canonical DB schema initializer."""

import os
from pathlib import Path

from db_connections import resolve_telemetry_db_path
from db_schema import (
    init_context_telemetry_database_sync,
    init_database_sync,
    init_timer_database_sync,
)

RUNTIME_DATABASE_DIR = Path(
    os.environ.get("TOKEN_API_DATABASE_DIR", Path.home() / "runtimes" / "database")
).expanduser()

DB_PATH = Path(
    os.environ.get("TOKEN_API_AGENTS_DB")
    or os.environ.get("TOKEN_API_DB")
    or RUNTIME_DATABASE_DIR / "agents.db"
).expanduser()
TIMER_DB_PATH = Path(
    os.environ.get("TOKEN_API_TIMER_DB")
    or os.environ.get("TOKEN_API_DB")
    or RUNTIME_DATABASE_DIR / "timer.db"
).expanduser()
TELEMETRY_DB_PATH = resolve_telemetry_db_path()


def init_database() -> None:
    """Initialize SQLite database with required tables."""
    init_database_sync(DB_PATH)
    init_timer_database_sync(TIMER_DB_PATH)
    init_context_telemetry_database_sync(TELEMETRY_DB_PATH)


if __name__ == "__main__":
    init_database()
