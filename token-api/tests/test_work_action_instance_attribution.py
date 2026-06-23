"""work_action events carry instance_id and refresh the instance heartbeat.

Regression guard (2026-06-22 timer flatline). The hook-driven work_action write
path logged ``work_action`` rows with a NULL ``instance_id`` and never touched
``instances.last_activity``: the session id survived only as a ``note`` string,
so every instance's heartbeat froze and ``compute_work_state`` read 0 active
instances all afternoon while the Emperor was actively prompting.

Contract pinned here (write path only — ``compute_work_state`` is read elsewhere):

  - A human work_action (prompt_submit / ask_user_question / typing-guard) for a
    registered instance logs a ``work_action`` row whose ``instance_id`` resolves
    to that instance, and bumps its ``last_activity`` to ~now.
  - The bump mirrors compute_work_state's discount EXACTLY: a ``hook_driven``
    instance or a pane under a live ``automated_pane_activity`` marker is NOT
    bumped (the agent-to-agent idle-reset vector stays closed) — UNLESS a human is
    typing (``tmux-typing-guard`` source, or the global typing guard is hot).
  - A session id that resolves to no registered instance leaves the row
    unattributed (NULL) and bumps nothing, without raising.
  - The originating instance is still attributed in BOTH the discounted and the
    non-discounted case (instance_id set either way; only the bump differs).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import pytest

SESSION_ID = "sess-attr-1"
PANE = "%701"


def _insert_instance(db_path, *, last_activity: datetime, hook_driven: int = 0) -> None:
    """Insert one local mechanicus instance, filling every NOT-NULL column."""
    with sqlite3.connect(db_path) as conn:
        cols = conn.execute("PRAGMA table_info(legacy_instances)").fetchall()
        values: dict[str, Any] = {}
        for _cid, name, ctype, notnull, dflt, _pk in cols:
            if dflt is not None or not notnull:
                continue
            values[name] = 0 if ctype.upper() in ("INTEGER", "REAL") else "x"
        values.update(
            id=SESSION_ID,
            session_id=SESSION_ID,
            tab_name="admin",
            status="working",
            engine="claude",
            working_dir="/work",
            tmux_pane=PANE,
            device_id="Mac-Mini",
            last_activity=last_activity.isoformat(),
            legion="mechanicus",
            is_subagent=0,
            hook_driven=hook_driven,
        )
        keys = list(values)
        conn.execute(
            f"INSERT INTO legacy_instances ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
            [values[k] for k in keys],
        )
        conn.commit()


def _set_marker(db_path, ttl_s: float = 60.0) -> None:
    injected = datetime.now()
    expires = injected + timedelta(seconds=ttl_s)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM automated_pane_activity")
        conn.execute(
            "INSERT INTO automated_pane_activity (tmux_pane, injected_at, expires_at, source, verb)"
            " VALUES (?, ?, ?, ?, ?)",
            (PANE, injected.isoformat(), expires.isoformat(), "test", "send-keys"),
        )
        conn.commit()


def _last_work_action(db_path) -> tuple[str | None, dict]:
    """(instance_id, details) of the most recent work_action event row."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT instance_id, details FROM events WHERE event_type = 'work_action' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "expected a work_action event row"
    return row[0], json.loads(row[1] or "{}")


def _last_activity(db_path) -> datetime:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_activity FROM legacy_instances WHERE id = ?", (SESSION_ID,)
        ).fetchone()
    return datetime.fromisoformat(row[0])


@pytest.fixture
def no_typing(app_env, monkeypatch):
    """Default: no human at the keyboard, so the discount logic actually engages.

    _typing_guard_active shells out to tmux; pin it deterministically off so the
    hook_driven / marker discount is not masked by a stray live tmux client.
    """
    monkeypatch.setattr(app_env.main, "_typing_guard_active", lambda: False)
    return app_env


# (1) A human prompt_submit work_action attributes the row and bumps last_activity.
def test_human_work_action_attributes_and_bumps(no_typing) -> None:
    main = no_typing.main
    stale = datetime.now() - timedelta(hours=7)
    _insert_instance(no_typing.db_path, last_activity=stale)

    asyncio.run(main.hook_work_action_callback("prompt_submit", session_id=SESSION_ID))

    instance_id, details = _last_work_action(no_typing.db_path)
    assert instance_id == SESSION_ID
    # note synthesized from first-class session_id for compute_work_state's discount.
    assert details["note"] == f"session_id={SESSION_ID}"
    # Heartbeat advanced from the 7h-stale value to ~now.
    assert (datetime.now() - _last_activity(no_typing.db_path)).total_seconds() < 60


# (2) hook_driven instance: still attributed, but NOT bumped (agent-to-agent reflex).
def test_hook_driven_work_action_attributes_but_does_not_bump(no_typing) -> None:
    main = no_typing.main
    stale = datetime.now() - timedelta(hours=7)
    _insert_instance(no_typing.db_path, last_activity=stale, hook_driven=1)

    asyncio.run(main.hook_work_action_callback("prompt_submit", session_id=SESSION_ID))

    instance_id, _ = _last_work_action(no_typing.db_path)
    assert instance_id == SESSION_ID  # attribution still happens
    # last_activity stays frozen — the idle-reset vector remains closed.
    assert _last_activity(no_typing.db_path) == stale


# (3) Live automated_pane_activity marker: not bumped even when not hook_driven.
def test_automated_marker_work_action_does_not_bump(no_typing) -> None:
    main = no_typing.main
    stale = datetime.now() - timedelta(hours=7)
    _insert_instance(no_typing.db_path, last_activity=stale)
    _set_marker(no_typing.db_path)

    asyncio.run(main.hook_work_action_callback("prompt_submit", session_id=SESSION_ID))

    instance_id, _ = _last_work_action(no_typing.db_path)
    assert instance_id == SESSION_ID
    assert _last_activity(no_typing.db_path) == stale


# (4) A tmux-typing-guard source is a human keystroke — always anchors, even
#     against a hook_driven instance with a live marker.
def test_typing_guard_source_overrides_discount(no_typing) -> None:
    main = no_typing.main
    stale = datetime.now() - timedelta(hours=7)
    _insert_instance(no_typing.db_path, last_activity=stale, hook_driven=1)
    _set_marker(no_typing.db_path)

    asyncio.run(main.hook_work_action_callback("tmux-typing-guard", session_id=SESSION_ID))

    assert (datetime.now() - _last_activity(no_typing.db_path)).total_seconds() < 60


# (5) A hot global typing guard anchors a discounted pane too (the Emperor is
#     driving it by hand even though it is flagged hook_driven).
def test_hot_global_typing_guard_overrides_discount(app_env, monkeypatch) -> None:
    main = app_env.main
    monkeypatch.setattr(main, "_typing_guard_active", lambda: True)
    stale = datetime.now() - timedelta(hours=7)
    _insert_instance(app_env.db_path, last_activity=stale, hook_driven=1)

    asyncio.run(main.hook_work_action_callback("prompt_submit", session_id=SESSION_ID))

    assert (datetime.now() - _last_activity(app_env.db_path)).total_seconds() < 60


# (6) An unregistered session id: row stays unattributed, nothing bumps, no raise.
def test_unregistered_session_leaves_row_unattributed(no_typing) -> None:
    main = no_typing.main
    asyncio.run(main.hook_work_action_callback("prompt_submit", session_id="ghost-session"))

    instance_id, details = _last_work_action(no_typing.db_path)
    assert instance_id is None
    assert details["note"] == "session_id=ghost-session"


# (7b) End-to-end through the real HTTP endpoint: a POST carrying session_id
#      first-class resolves the instance, attributes the event, and bumps the
#      heartbeat (the live-endpoint path, not just the in-process callback).
def test_http_work_action_with_session_id_bumps(no_typing, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    main = no_typing.main

    async def _no_pane_rows():
        return []

    monkeypatch.setattr(main, "_tmux_pane_rows", _no_pane_rows)
    stale = datetime.now() - timedelta(hours=7)
    _insert_instance(no_typing.db_path, last_activity=stale)

    client = TestClient(main.app)
    resp = client.post("/api/work-action", json={"source": "stream-deck", "session_id": SESSION_ID})
    assert resp.status_code == 200, resp.text

    instance_id, _ = _last_work_action(no_typing.db_path)
    assert instance_id == SESSION_ID
    assert (datetime.now() - _last_activity(no_typing.db_path)).total_seconds() < 60


# (8) Legacy back-compat: session id smuggled in the note still resolves+bumps.
def test_legacy_note_session_id_still_resolves(no_typing) -> None:
    main = no_typing.main
    stale = datetime.now() - timedelta(hours=7)
    _insert_instance(no_typing.db_path, last_activity=stale)

    asyncio.run(main.hook_work_action_callback("prompt_submit", f"session_id={SESSION_ID}"))

    instance_id, _ = _last_work_action(no_typing.db_path)
    assert instance_id == SESSION_ID
    assert (datetime.now() - _last_activity(no_typing.db_path)).total_seconds() < 60
