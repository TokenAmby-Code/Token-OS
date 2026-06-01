"""Gap 1 (D2 backstop) — dormant `worktrees` table + partial-unique index.

This schema is MERGED but DORMANT: it only takes effect when token-api next
runs init_database_async() at startup. We unit-test it against a throwaway DB
(never the live ~/.claude/agents.db) per the activation rule.
"""

import sqlite3
from pathlib import Path

import db_schema


def _init(tmp_path: Path) -> Path:
    db_path = tmp_path / "throwaway-test.db"
    db_schema.init_database_sync(db_path)
    return db_path


def test_worktrees_table_created(tmp_path):
    db_path = _init(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='worktrees'"
        ).fetchone()
        assert row is not None, "worktrees table should exist"
    finally:
        conn.close()


def test_active_unique_index_created(tmp_path):
    db_path = _init(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_worktrees_active_unique'"
        ).fetchone()
        assert row is not None, "idx_worktrees_active_unique should exist"
    finally:
        conn.close()


def test_partial_unique_blocks_two_active_same_branch(tmp_path):
    db_path = _init(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO worktrees(project, branch, path, status) "
            "VALUES('Token-OS', 'feat', '/wt-feat', 'active')"
        )
        conn.commit()
        with __import__("pytest").raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO worktrees(project, branch, path, status) "
                "VALUES('Token-OS', 'feat', '/wt-feat-2', 'active')"
            )
            conn.commit()
    finally:
        conn.close()


def test_partial_unique_allows_active_plus_inactive(tmp_path):
    db_path = _init(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO worktrees(project, branch, path, status) "
            "VALUES('Token-OS', 'feat', '/wt-feat', 'deleted')"
        )
        conn.execute(
            "INSERT INTO worktrees(project, branch, path, status) "
            "VALUES('Token-OS', 'feat', '/wt-feat-2', 'active')"
        )
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM worktrees WHERE project='Token-OS' AND branch='feat'"
        ).fetchone()[0]
        assert n == 2
    finally:
        conn.close()
