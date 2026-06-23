"""Deferred interactive session docs + never-acted reap (PR1a).

SessionStart no longer mints a "Needs Session Name" placeholder for a genuine
interactive pane: resolve_session_doc_for_start returns (None,
"interactive_deferred"), and the placeholder is minted lazily on the first real
prompt (handle_prompt_submit), pragma-once. A pane that is opened and closed
without a prompt therefore leaves NO doc and is soft-archived on SessionEnd
(status='archived', rank='retired') instead of accumulating an empty `stopped`
orphan row. Dispatched/cron workers (whose docs resolve elsewhere, and which may
legitimately carry a NULL session_doc_id) are never mistaken for un-named human
panes.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys

import aiosqlite


def _insert_instance(
    db_path,
    instance_id,
    *,
    origin_type="local",
    commander_type="emperor",
    persona_id=None,
    is_subagent=0,
    automated=0,
    golden_throne=None,
    dispatch_session_doc_path=None,
    session_doc_id=None,
    status="idle",
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO instances
           (id, name, device_id, origin_type, commander_type, persona_id,
            is_subagent, automated, golden_throne, dispatch_session_doc_path,
            session_doc_id, status, rank)
           VALUES (?, ?, 'Mac-Mini', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'astartes')""",
        (
            instance_id,
            instance_id,
            origin_type,
            commander_type,
            persona_id,
            is_subagent,
            automated,
            golden_throne,
            dispatch_session_doc_path,
            session_doc_id,
            status,
        ),
    )
    conn.commit()
    conn.close()


def _row(db_path, instance_id) -> sqlite3.Row | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
    conn.close()
    return r


def _doc_count(db_path) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM session_documents").fetchone()[0]
    conn.close()
    return n


async def _never_dead(db, session_id, existing, actor) -> bool:
    return False


# ── SessionStart defers (no eager placeholder) ─────────────────────────────────


def test_resolve_interactive_defers_without_minting(app_env) -> None:
    """A plain interactive launch returns the deferred sentinel and mints no doc."""
    helpers = sys.modules.get("session_doc_helpers") or __import__("session_doc_helpers")

    async def run():
        async with aiosqlite.connect(app_env.db_path) as db:
            return await helpers.resolve_session_doc_for_start(
                db,
                dispatch_session_doc_path=None,
                primarch_name=None,
                origin_type="local",
                cron_job_id=None,
                cron_job_name=None,
                working_dir="/tmp",
                is_subagent=False,
                legion=None,
            )

    doc_id, policy = asyncio.run(run())
    assert doc_id is None
    assert policy == "interactive_deferred"
    assert _doc_count(app_env.db_path) == 0


def test_resolve_dispatch_still_unresolved(app_env) -> None:
    """An automated launch that can't resolve its doc still surfaces the miss
    (not the new interactive sentinel)."""
    helpers = sys.modules.get("session_doc_helpers") or __import__("session_doc_helpers")

    async def run():
        async with aiosqlite.connect(app_env.db_path) as db:
            return await helpers.resolve_session_doc_for_start(
                db,
                dispatch_session_doc_path=None,
                primarch_name="some-primarch",
                origin_type="dispatch",
                cron_job_id=None,
                cron_job_name=None,
                working_dir="/tmp",
                is_subagent=False,
                legion=None,
            )

    doc_id, policy = asyncio.run(run())
    assert doc_id is None
    assert policy == "unresolved_dispatch"
    assert _doc_count(app_env.db_path) == 0


# ── First prompt mints the doc once (pragma-once) ──────────────────────────────


def test_first_prompt_mints_doc_once(app_env, monkeypatch) -> None:
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_stop_if_dead_pane", _never_dead)
    _insert_instance(app_env.db_path, "inter-1", session_doc_id=None)

    async def run():
        return await hooks.handle_prompt_submit({"session_id": "inter-1"})

    res1 = asyncio.run(run())
    assert res1["success"] is True
    row1 = _row(app_env.db_path, "inter-1")
    assert row1["status"] == "working"
    assert row1["session_doc_id"] is not None
    assert _doc_count(app_env.db_path) == 1
    minted = row1["session_doc_id"]

    # Second prompt must NOT mint again (pragma-once via session_doc_id IS NULL gate).
    asyncio.run(run())
    row2 = _row(app_env.db_path, "inter-1")
    assert row2["session_doc_id"] == minted
    assert _doc_count(app_env.db_path) == 1


def test_dispatched_null_doc_not_minted_on_prompt(app_env, monkeypatch) -> None:
    """A dispatched worker with a legitimately-NULL doc must never get an
    interactive placeholder on its first prompt."""
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_stop_if_dead_pane", _never_dead)
    _insert_instance(app_env.db_path, "disp-1", origin_type="dispatch", session_doc_id=None)

    async def run():
        return await hooks.handle_prompt_submit({"session_id": "disp-1"})

    asyncio.run(run())
    row = _row(app_env.db_path, "disp-1")
    assert row["status"] == "working"
    assert row["session_doc_id"] is None
    assert _doc_count(app_env.db_path) == 0


# ── SessionEnd reaps the never-acted interactive pane ──────────────────────────


def _run_session_end(hooks, monkeypatch, session_id) -> dict:
    monkeypatch.setattr(hooks, "_spawn_session_end_assertion", lambda *a, **k: None)
    monkeypatch.setattr(hooks, "_schedule_naming_nudge", lambda *a, **k: None)
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: None)

    async def run():
        return await hooks.handle_session_end({"session_id": session_id})

    return asyncio.run(run())


def test_session_end_soft_archives_never_acted_interactive(app_env, monkeypatch) -> None:
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "ghost-1", session_doc_id=None)

    _run_session_end(hooks, monkeypatch, "ghost-1")

    row = _row(app_env.db_path, "ghost-1")
    assert row["status"] == "archived"
    assert row["rank"] == "retired"
    assert row["archived_at"] is not None


def test_session_end_keeps_acted_interactive_stopped(app_env, monkeypatch) -> None:
    """An interactive pane that DID act (has a doc) closes normally as stopped."""
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "worked-1", session_doc_id=4242)

    _run_session_end(hooks, monkeypatch, "worked-1")

    row = _row(app_env.db_path, "worked-1")
    assert row["status"] == "stopped"


def test_session_end_working_null_doc_not_reaped(app_env, monkeypatch) -> None:
    """A pane that ever worked (status='working') has acted even if its doc is
    NULL — it must close as stopped, never soft-archived. 'never acted' is
    status=='idle' AND doc IS NULL, not doc IS NULL alone."""
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "busy-1", status="working", session_doc_id=None)

    _run_session_end(hooks, monkeypatch, "busy-1")

    row = _row(app_env.db_path, "busy-1")
    assert row["status"] == "stopped"


def test_session_end_dispatch_null_doc_not_reaped(app_env, monkeypatch) -> None:
    """A dispatched worker with a NULL doc is not interactive — close as stopped,
    never soft-archived."""
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "disp-end-1", origin_type="dispatch", session_doc_id=None)

    _run_session_end(hooks, monkeypatch, "disp-end-1")

    row = _row(app_env.db_path, "disp-end-1")
    assert row["status"] == "stopped"
