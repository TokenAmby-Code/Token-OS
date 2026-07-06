import sqlite3
from pathlib import Path

import db_schema

TIMER_TABLES = {
    "timer_state",
    "timer_state_daily",
    "timer_sessions",
    "timer_mode_changes",
    "timer_daily_scores",
    "timer_shifts",
    "timer_samples",
}
CONTEXT_TELEMETRY_TABLES = {
    "context_governor_state",
    "context_governor_audit",
}


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()


def test_fresh_agents_db_init_does_not_create_timer_tables(tmp_path: Path) -> None:
    agents_db = tmp_path / "agents.db"

    db_schema.init_database_sync(agents_db)

    assert _table_names(agents_db).isdisjoint(TIMER_TABLES)


def test_fresh_timer_db_init_creates_timer_tables(tmp_path: Path) -> None:
    timer_db = tmp_path / "timer.db"

    db_schema.init_timer_database_sync(timer_db)

    assert TIMER_TABLES <= _table_names(timer_db)


def test_context_governor_tables_live_in_telemetry_db_not_agents_db(tmp_path: Path) -> None:
    agents_db = tmp_path / "agents.db"
    telemetry_db = tmp_path / "telemetry.db"

    db_schema.init_database_sync(agents_db)
    db_schema.init_context_telemetry_database_sync(telemetry_db)

    assert _table_names(agents_db).isdisjoint(CONTEXT_TELEMETRY_TABLES)
    assert CONTEXT_TELEMETRY_TABLES <= _table_names(telemetry_db)
