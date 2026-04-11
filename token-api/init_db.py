#!/usr/bin/env python3
"""Standalone wrapper for the canonical DB schema initializer."""

import os
from pathlib import Path

from db_schema import init_database_sync

DB_PATH = Path(os.environ.get("TOKEN_API_DB", Path.home() / ".claude" / "agents.db"))


def init_database():
    """Initialize SQLite database with required tables."""
    init_database_sync(DB_PATH)


if __name__ == "__main__":
    init_database()
