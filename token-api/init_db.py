#!/usr/bin/env python3
"""Standalone wrapper for the canonical DB schema initializer."""

import os
from pathlib import Path

from db_schema import init_database_sync, init_timer_database_sync

RUNTIME_DATABASE_DIR = Path(
    os.environ.get("TOKEN_API_DATABASE_DIR", Path.home() / "runtimes" / "database")
).expanduser()
LEGACY_AGENTS_DB_PATH = Path.home() / ".claude" / "agents.db"


def _legacy_token_api_db_unless_live() -> str | None:
    value = os.environ.get("TOKEN_API_DB")
    if not value:
        return None
    path = Path(value).expanduser()
    if path.resolve() == LEGACY_AGENTS_DB_PATH.resolve():
        return None
    return value


DB_PATH = Path(
    os.environ.get("TOKEN_API_AGENTS_DB")
    or _legacy_token_api_db_unless_live()
    or RUNTIME_DATABASE_DIR / "agents.db"
).expanduser()
TIMER_DB_PATH = Path(
    os.environ.get("TOKEN_API_TIMER_DB")
    or _legacy_token_api_db_unless_live()
    or RUNTIME_DATABASE_DIR / "timer.db"
).expanduser()


def init_database():
    """Initialize SQLite database with required tables."""
    init_database_sync(DB_PATH)
    init_timer_database_sync(TIMER_DB_PATH)


if __name__ == "__main__":
    init_database()
