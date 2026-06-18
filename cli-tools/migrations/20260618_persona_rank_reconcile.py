#!/usr/bin/env python3
"""Emperor-gated Step 3 live DB reconcile.

Review-only migration. Do not run against the live DB without the merge/deploy gate.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DEAD_PROFILE_SLUGS = ("profile_1", "profile_3", "profile_5", "profile_7", "profile_8")


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(db: Path, *, dry_run: bool = True) -> None:
    if not db.exists():
        raise FileNotFoundError(f"database does not exist: {db}")
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        conn.execute("UPDATE personas SET default_rank = 'astartes' WHERE slug = 'inquisitor'")
        if "persona_lock" in columns(conn, "instances"):
            conn.execute(
                """
                UPDATE instances
                   SET persona_lock = NULL
                 WHERE persona_id IN (SELECT id FROM personas WHERE slug = 'inquisitor')
                """
            )
        conn.execute(
            f"DELETE FROM personas WHERE slug IN ({','.join('?' for _ in DEAD_PROFILE_SLUGS)})",
            DEAD_PROFILE_SLUGS,
        )
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db", type=Path)
    parser.add_argument("--apply", action="store_true", help="commit changes (gate-required)")
    args = parser.parse_args()
    migrate(args.db, dry_run=not args.apply)
    print("dry-run complete" if not args.apply else "migration applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
