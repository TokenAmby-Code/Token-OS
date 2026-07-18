#!/usr/bin/env python3
"""Regression validation for session-doc SQLite lock release.

Run directly (pytest is intentionally absent from this repository):

    cd token-api && uv run python validate_session_doc_lock_release.py

The fake Obsidian facade performs an independent SQLite write while each create
endpoint is awaiting it. That write can succeed only if the endpoint committed
and released its reservation transaction before crossing the external-I/O
boundary.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiosqlite  # noqa: E402

import main  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    marker = "PASS" if condition else "FAIL"
    print(f"[{marker}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def initialize_db(path: Path, *, instance_id: str | None = None) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE session_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                file_path TEXT NOT NULL,
                project TEXT,
                primarch_name TEXT,
                branch TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE facade_probe (value TEXT NOT NULL);
            CREATE TABLE instances (
                id TEXT PRIMARY KEY,
                session_doc_id INTEGER,
                session_doc_policy TEXT,
                continuity_binding_source TEXT,
                workflow_state TEXT
            );
            """
        )
        if instance_id:
            db.execute(
                "INSERT INTO instances (id, workflow_state) VALUES (?, 'working')",
                (instance_id,),
            )
        db.commit()


async def exercise_endpoint(*, bind_instance: bool) -> tuple[int, int | None]:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db_path = root / "agents.db"
        instance_id = "instance-1" if bind_instance else None
        initialize_db(db_path, instance_id=instance_id)

        original_db_path = main.DB_PATH
        original_facade = main._session_docs_facade
        original_log_event = main.log_event
        original_update_instance = main.update_instance
        original_orphan = main._handle_orphan_doc

        async def facade_probe(operation, _file_path, **_payload):
            if operation == "create":
                # Fail fast instead of waiting through the production busy
                # timeout if an endpoint still owns a write transaction here.
                async with aiosqlite.connect(db_path, timeout=0.05) as db:
                    await db.execute("INSERT INTO facade_probe (value) VALUES ('released')")
                    await db.commit()
            return {}

        async def quiet_log_event(*_args, **_kwargs):
            return None

        async def fake_update_instance(db, *, instance_id, updates, **_kwargs):
            await db.execute(
                """UPDATE instances
                   SET session_doc_id = ?, session_doc_policy = ?,
                       continuity_binding_source = ?
                   WHERE id = ?""",
                (
                    updates["session_doc_id"],
                    updates["session_doc_policy"],
                    updates["continuity_binding_source"],
                    instance_id,
                ),
            )

        async def quiet_orphan(_doc_id):
            return None

        main.DB_PATH = db_path
        main._session_docs_facade = facade_probe  # type: ignore[assignment]
        main.log_event = quiet_log_event  # type: ignore[assignment]
        main.update_instance = fake_update_instance  # type: ignore[assignment]
        main._handle_orphan_doc = quiet_orphan  # type: ignore[assignment]
        try:
            request = main.SessionDocCreateRequest(
                title="Lock release",
                file_path=str(root / "lock-release.md"),
            )
            if instance_id:
                result = await main.create_doc_for_instance(instance_id, request)
            else:
                result = await main.create_session_doc(request)
        finally:
            main.DB_PATH = original_db_path
            main._session_docs_facade = original_facade  # type: ignore[assignment]
            main.log_event = original_log_event  # type: ignore[assignment]
            main.update_instance = original_update_instance  # type: ignore[assignment]
            main._handle_orphan_doc = original_orphan  # type: ignore[assignment]

        with sqlite3.connect(db_path) as db:
            probe_count = db.execute("SELECT COUNT(*) FROM facade_probe").fetchone()[0]
            bound_doc_id = None
            if instance_id:
                bound_doc_id = db.execute(
                    "SELECT session_doc_id FROM instances WHERE id = ?", (instance_id,)
                ).fetchone()[0]
        return probe_count, bound_doc_id or result["id"]


async def main_async() -> int:
    probe_count, doc_id = await exercise_endpoint(bind_instance=False)
    check("create_session_doc releases SQLite before facade", probe_count == 1, probe_count)
    check("create_session_doc retains reservation", bool(doc_id), doc_id)

    probe_count, bound_doc_id = await exercise_endpoint(bind_instance=True)
    check("create_doc_for_instance releases SQLite before facade", probe_count == 1, probe_count)
    check("create_doc_for_instance binds reserved doc", bool(bound_doc_id), bound_doc_id)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
