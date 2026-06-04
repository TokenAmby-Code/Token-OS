"""Slice A of the tmuxctl pane-ownership cutover: ``pane_state_worker`` resolves
``instance_id -> pane`` LIVE at dequeue and fails closed.

tmuxctl is the sole owner of ``instance_id -> pane``. The pane-state queue (the
``@CC_STATE``/``@PLANNING_STATE`` pusher) must no longer trust the stored
``pane_state_queue.tmux_pane`` / ``claude_instances.tmux_pane`` column: it
resolves the live pane per row via ``shared.resolve_instance_pane`` and:

  * delivers ``tmux set-option`` to the live-resolved pane, never the stored one;
  * fails closed when the pane no longer resolves — no ``set-option``, no
    close-down assertion — while still draining the queue row so a dead instance
    cannot wedge the queue;
  * keys the ``@CC_STATE=stopped`` assert-persona decision on the *live role*
    from the resolver, not the stored ``pane_label``.

File-scoped resolver stub (mirrors the proven Tier 2(b) pattern) so the real
resolver elsewhere is untouched; each test sets the live-resolution it asserts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _enqueue_pane_state(
    db_path: Path, instance_id: str, variable: str, value: str, tmux_pane: str | None
) -> int:
    """Insert one row into pane_state_queue (the SQLite trigger's product) and
    return its id. Mirrors what ``trg_status_pane_state`` writes on a status flip,
    deliberately stamping a (now possibly stale) stored ``tmux_pane``."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO pane_state_queue (instance_id, variable, value, tmux_pane) "
            "VALUES (?, ?, ?, ?)",
            (instance_id, variable, value, tmux_pane),
        )
        conn.commit()
        return int(cur.lastrowid)


def _queue_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM pane_state_queue").fetchone()[0])


@pytest.fixture
def _capture_set_option(app_env, monkeypatch) -> list[tuple[str, ...]]:
    """Capture every ``tmux set-option`` the worker issues (no tmux server in
    tests). Returns the list of captured argv tuples."""
    main = app_env.main
    calls: list[tuple[str, ...]] = []

    async def _fake_offloop(cmd, **kwargs):
        calls.append(tuple(cmd))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(main, "_run_subprocess_offloop", _fake_offloop)
    return calls


def _set_options(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [c for c in calls if len(c) >= 2 and c[0] == "tmux" and c[1] == "set-option"]


# ---- live-resolved pane wins over the stored column -------------------------


async def test_pushes_to_live_resolved_pane_not_stored_column(
    app_env: Any, monkeypatch: Any, _capture_set_option: list
) -> None:
    """A row stamped with a now-stale ``%999`` must push ``@CC_STATE`` to the
    live-resolved ``%77`` (pane moved/reused since the trigger fired)."""
    main = app_env.main

    async def _resolve_live(_instance_id):
        return ("%77", "palace:N")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_live)

    _enqueue_pane_state(app_env.db_path, "inst-moved", "@CC_STATE", "working", "%999")
    results = await main.process_pane_state_queue_once()

    sets = _set_options(_capture_set_option)
    assert len(sets) == 1
    argv = sets[0]
    assert "%77" in argv, "must push to the live-resolved pane, not stored %999"
    assert "%999" not in argv
    assert argv[-2:] == ("@CC_STATE", "working")
    assert results[0]["tmux_pane"] == "%77"
    assert results[0]["status"] == "applied"
    # Row drained.
    assert _queue_count(app_env.db_path) == 0


# ---- fail closed when the pane is gone --------------------------------------


async def test_pane_gone_drains_row_without_touching_tmux(
    app_env: Any, monkeypatch: Any, _capture_set_option: list
) -> None:
    """If the instance no longer resolves to a pane, the worker issues no
    ``set-option`` yet still drains the queue row (no wedge)."""
    main = app_env.main

    async def _gone(_instance_id):
        return (None, None)

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _gone)

    spawned: list[Any] = []
    monkeypatch.setattr(main, "spawn_tmux_assert_instance", lambda *a, **k: spawned.append((a, k)))

    _enqueue_pane_state(app_env.db_path, "inst-gone", "@CC_STATE", "stopped", "%999")
    results = await main.process_pane_state_queue_once()

    assert _set_options(_capture_set_option) == [], "vanished pane must get no set-option"
    assert spawned == [], "vanished pane must not spawn a close-down assertion"
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == "pane_unresolved"
    assert results[0]["tmux_pane"] is None
    # The row is still drained so a dead instance cannot wedge the queue.
    assert _queue_count(app_env.db_path) == 0


# ---- @CC_STATE=stopped spawns the assertion keyed on the LIVE role ----------


async def test_stopped_spawns_assertion_with_live_role(
    app_env: Any, monkeypatch: Any, _capture_set_option: list
) -> None:
    """A live-resolved stopped instance with a non-persona role spawns the
    close-down assertion targeting the live role (resolved, not stored)."""
    main = app_env.main

    async def _resolve_live(_instance_id):
        return ("%77", "palace:N")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_live)

    spawned: list[tuple] = []
    monkeypatch.setattr(
        main,
        "spawn_tmux_assert_instance",
        lambda pane_target, instance_id="", source="system": spawned.append(
            (pane_target, instance_id, source)
        ),
    )

    _enqueue_pane_state(app_env.db_path, "inst-stop", "@CC_STATE", "stopped", "%999")
    await main.process_pane_state_queue_once()

    assert len(spawned) == 1
    pane_target, instance_id, _source = spawned[0]
    assert pane_target == "palace:N", "assertion must target the LIVE role"
    assert instance_id == "inst-stop"


async def test_stopped_on_persona_role_skips_assertion(
    app_env: Any, monkeypatch: Any, _capture_set_option: list
) -> None:
    """The assert-persona guard keys on the LIVE role: a stopped instance whose
    live role is a persona label (e.g. ``legion:custodes``) must NOT spawn an
    assertion — even though the stored column never enters the decision."""
    main = app_env.main

    async def _resolve_persona(_instance_id):
        return ("%5", "legion:custodes")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_persona)

    spawned: list[Any] = []
    monkeypatch.setattr(main, "spawn_tmux_assert_instance", lambda *a, **k: spawned.append((a, k)))

    _enqueue_pane_state(app_env.db_path, "inst-custodes", "@CC_STATE", "stopped", "%5")
    await main.process_pane_state_queue_once()

    assert spawned == [], "persona role must not spawn a close-down assertion"


async def test_bounced_state_in_one_drain_does_not_assert_stale_stopped(
    app_env: Any, monkeypatch: Any, _capture_set_option: list
) -> None:
    """A status that bounces stopped -> idle within one drain queues two @CC_STATE
    rows. The early ``stopped`` must NOT fire a close-down assertion when the FINAL
    drained state for that instance is no longer stopped."""
    main = app_env.main

    async def _resolve_live(_instance_id):
        return ("%77", "palace:N")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_live)

    spawned: list[Any] = []
    monkeypatch.setattr(main, "spawn_tmux_assert_instance", lambda *a, **k: spawned.append((a, k)))

    # Same instance: stopped then idle, both drained in one batch (ORDER BY id).
    _enqueue_pane_state(app_env.db_path, "inst-bounce", "@CC_STATE", "stopped", "%77")
    _enqueue_pane_state(app_env.db_path, "inst-bounce", "@CC_STATE", "idle", "%77")
    await main.process_pane_state_queue_once()

    assert spawned == [], "final state is idle — no stale stopped assertion"


async def test_repeated_stopped_in_one_drain_asserts_once(
    app_env: Any, monkeypatch: Any, _capture_set_option: list
) -> None:
    """Two ``stopped`` rows for the same instance in one drain collapse to a single
    close-down assertion (deduped on the final per-instance state)."""
    main = app_env.main

    async def _resolve_live(_instance_id):
        return ("%77", "palace:N")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_live)

    spawned: list[tuple] = []
    monkeypatch.setattr(
        main,
        "spawn_tmux_assert_instance",
        lambda pane_target, instance_id="", source="system": spawned.append(
            (pane_target, instance_id)
        ),
    )

    _enqueue_pane_state(app_env.db_path, "inst-stop2", "@CC_STATE", "stopped", "%77")
    _enqueue_pane_state(app_env.db_path, "inst-stop2", "@CC_STATE", "stopped", "%77")
    await main.process_pane_state_queue_once()

    assert spawned == [("palace:N", "inst-stop2")], "stopped must assert exactly once"
