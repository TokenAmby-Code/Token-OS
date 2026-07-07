"""``pane_state_worker`` — live pane resolution + the semantic-rename cutover.

tmuxctl is the sole owner of ``instance_id -> pane``. The pane-state queue resolves
the live pane per row via ``shared.resolve_instance_pane`` and:

  * delivers the variable to the live-resolved pane, never a stored one;
  * fails closed when the pane no longer resolves — no write — while still draining
    the queue row so a dead instance cannot wedge the queue.

Kill-order cutover (this change): a ``@PANE_LABEL`` row no longer authors a raw
``set-option @PANE_LABEL`` through ``/tmux/run``. It routes through the semantic
``shared.tmuxctld_rename_pane`` (tmuxctld ``POST /instance/rename``), which owns BOTH
the border nametag and the native pane title. Every OTHER variable (``@CC_STATE``,
``@PLANNING_STATE``, ...) keeps the generic ``set-option`` path — the regression guard
below pins that split.

PHASE B sever: this worker makes ZERO tmux kill decisions and spawns no subprocess.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _enqueue_pane_state(db_path: Path, instance_id: str, variable: str, value: str) -> int:
    """Insert one row into pane_state_queue (the SQLite trigger's product) and
    return its id. Pane geometry is resolved LIVE at dequeue — the queue stamps no pane."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO pane_state_queue (instance_id, variable, value) VALUES (?, ?, ?)",
            (instance_id, variable, value),
        )
        conn.commit()
        return int(cur.lastrowid)


def _queue_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM pane_state_queue").fetchone()[0])


def _insert_instance(db_path: Path, instance_id: str, name: str) -> None:
    """Insert a minimal canonical ``instances`` row so the rename trigger has a target."""
    now = "2026-07-07T00:00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO instances
               (id, name, working_dir, origin_type, device_id, status,
                created_at, last_activity)
               VALUES (?, ?, '/tmp', 'local', 'Mac-Mini', 'idle', ?, ?)""",
            (instance_id, name, now, now),
        )
        conn.commit()


def _queue_rows(db_path: Path, variable: str) -> list[tuple[str, str, str]]:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT instance_id, variable, value FROM pane_state_queue "
            "WHERE variable = ? ORDER BY id",
            (variable,),
        )
        return [tuple(r) for r in cur.fetchall()]


@pytest.fixture
def _seams(app_env: Any, monkeypatch: Any) -> SimpleNamespace:
    """Capture the two live-write seams the worker uses (no tmux server in tests):

    * ``shared.tmuxctld_run_tmux`` — the generic ``set-option`` path;
    * ``shared.tmuxctld_rename_pane`` — the semantic ``@PANE_LABEL`` rename path.

    Both are recorded so a test can assert which seam a given variable routed through.
    """
    main = app_env.main
    set_options: list[tuple[str, ...]] = []
    renames: list[dict] = []

    async def _fake_run_tmux(args, **kwargs):
        set_options.append(tuple(args))
        return {"stdout": ""}

    async def _fake_rename(*, instance_id=None, pane=None, name):
        renames.append({"instance_id": instance_id, "pane": pane, "name": name})
        return {"ok": True, "result": {"found": True, "target": pane, "name": name}}

    monkeypatch.setattr(main.shared, "tmuxctld_run_tmux", _fake_run_tmux)
    monkeypatch.setattr(main.shared, "tmuxctld_rename_pane", _fake_rename)
    return SimpleNamespace(set_options=set_options, renames=renames)


# ---- @PANE_LABEL routes through the semantic rename, not raw set-option ------


async def test_pane_label_routes_through_semantic_rename(
    app_env: Any, monkeypatch: Any, _seams: SimpleNamespace
) -> None:
    """A ``@PANE_LABEL`` row calls ``tmuxctld_rename_pane`` with the live-resolved pane
    and NEVER authors a raw ``set-option @PANE_LABEL`` (token-api is no longer a writer
    of pane identity)."""
    main = app_env.main

    async def _resolve_live(_instance_id):
        return ("%12", "palace:N")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_live)

    _enqueue_pane_state(app_env.db_path, "inst-named", "@PANE_LABEL", "auth-refactor")
    results = await main.process_pane_state_queue_once()

    assert _seams.set_options == [], "no raw set-option @PANE_LABEL may be authored"
    assert _seams.renames == [{"instance_id": None, "pane": "%12", "name": "auth-refactor"}], (
        "must route through the semantic rename with the live-resolved pane"
    )
    assert results[0]["status"] == "applied"
    assert results[0]["tmux_pane"] == "%12"
    assert _queue_count(app_env.db_path) == 0


async def test_pane_label_rename_failure_marks_row_failed(
    app_env: Any, monkeypatch: Any, _seams: SimpleNamespace
) -> None:
    """A ``not ok`` daemon envelope from the rename marks the row failed (mirrors the
    generic path's ``result is None`` handling). The row still drains."""
    main = app_env.main

    async def _resolve_live(_instance_id):
        return ("%12", "palace:N")

    async def _rename_fail(*, instance_id=None, pane=None, name):
        return None  # transport failure / absent daemon

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_live)
    monkeypatch.setattr(main.shared, "tmuxctld_rename_pane", _rename_fail)

    _enqueue_pane_state(app_env.db_path, "inst-named", "@PANE_LABEL", "auth-refactor")
    results = await main.process_pane_state_queue_once()

    assert results[0]["status"] == "failed"
    assert _queue_count(app_env.db_path) == 0


async def test_pane_label_rename_fail_closed_found_false_marks_row_failed(
    app_env: Any, monkeypatch: Any, _seams: SimpleNamespace
) -> None:
    """The daemon fails closed for a vanished pane with ``ok=True`` but
    ``result.found=False`` — the HTTP call succeeded, yet NO rename happened. The
    worker must NOT record that as applied (else a dead pane looks renamed); it marks
    the row failed. The row still drains."""
    main = app_env.main

    async def _resolve_live(_instance_id):
        return ("%12", "palace:N")

    async def _rename_fail_closed(*, instance_id=None, pane=None, name):
        return {"ok": True, "result": {"found": False, "target": pane, "name": name}}

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_live)
    monkeypatch.setattr(main.shared, "tmuxctld_rename_pane", _rename_fail_closed)

    _enqueue_pane_state(app_env.db_path, "inst-named", "@PANE_LABEL", "auth-refactor")
    results = await main.process_pane_state_queue_once()

    assert results[0]["status"] == "failed", "ok=True but found=False must not be 'applied'"
    assert _queue_count(app_env.db_path) == 0


# ---- @CC_STATE / @PLANNING_STATE keep the generic set-option path -----------


async def test_cc_state_takes_generic_set_option_path(
    app_env: Any, monkeypatch: Any, _seams: SimpleNamespace
) -> None:
    """Regression guard: ``@CC_STATE`` stays on the generic ``set-option`` path and does
    NOT route through the rename endpoint."""
    main = app_env.main

    async def _resolve_live(_instance_id):
        return ("%77", "palace:N")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_live)

    _enqueue_pane_state(app_env.db_path, "inst-moved", "@CC_STATE", "working")
    results = await main.process_pane_state_queue_once()

    assert _seams.renames == [], "@CC_STATE must not touch the rename endpoint"
    assert _seams.set_options == [("set-option", "-p", "-t", "%77", "@CC_STATE", "working")]
    assert results[0]["status"] == "applied"
    assert results[0]["tmux_pane"] == "%77"
    assert _queue_count(app_env.db_path) == 0


async def test_planning_state_takes_generic_set_option_path(
    app_env: Any, monkeypatch: Any, _seams: SimpleNamespace
) -> None:
    """Regression guard: ``@PLANNING_STATE`` stays on the generic ``set-option`` path."""
    main = app_env.main

    async def _resolve_live(_instance_id):
        return ("%9", "council:custodes")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_live)

    _enqueue_pane_state(app_env.db_path, "inst-plan", "@PLANNING_STATE", "planning")
    await main.process_pane_state_queue_once()

    assert _seams.renames == [], "@PLANNING_STATE must not touch the rename endpoint"
    assert _seams.set_options == [("set-option", "-p", "-t", "%9", "@PLANNING_STATE", "planning")]


async def test_generic_push_goes_to_live_resolved_pane_not_stored(
    app_env: Any, monkeypatch: Any, _seams: SimpleNamespace
) -> None:
    """A generic variable pushes to the live-resolved pane (``%77``), never a stale one."""
    main = app_env.main

    async def _resolve_live(_instance_id):
        return ("%77", "palace:N")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_live)

    _enqueue_pane_state(app_env.db_path, "inst-moved", "@CC_STATE", "working")
    await main.process_pane_state_queue_once()

    argv = _seams.set_options[0]
    assert "%77" in argv and "%999" not in argv


# ---- fail closed when the pane is gone --------------------------------------


async def test_pane_gone_drains_row_without_touching_tmux(
    app_env: Any, monkeypatch: Any, _seams: SimpleNamespace
) -> None:
    """If the instance no longer resolves to a pane, the worker issues neither a
    ``set-option`` NOR a rename, yet still drains the queue row (no wedge)."""
    main = app_env.main

    async def _gone(_instance_id):
        return (None, None)

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _gone)

    _enqueue_pane_state(app_env.db_path, "inst-gone", "@PANE_LABEL", "some-name")
    results = await main.process_pane_state_queue_once()

    assert _seams.set_options == [], "vanished pane must get no set-option"
    assert _seams.renames == [], "vanished pane must get no rename"
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == "pane_unresolved"
    assert results[0]["tmux_pane"] is None
    assert _queue_count(app_env.db_path) == 0


# ---- token-api makes ZERO tmux kill decisions (PHASE B sever) ---------------


async def test_worker_spawns_no_subprocess(
    app_env: Any, monkeypatch: Any, _seams: SimpleNamespace
) -> None:
    """Draining any row (including ``@CC_STATE=stopped``) spawns no close-down
    subprocess — the worker never reaches across to assert/kill a pane."""
    main = app_env.main
    popen_calls: list[tuple] = []

    def _spy_popen(args, *_a, **_k):
        popen_calls.append(tuple(args) if isinstance(args, (list, tuple)) else (args,))
        return None

    async def _resolve_live(_instance_id):
        return ("%77", "palace:N")

    monkeypatch.setattr(main.subprocess, "Popen", _spy_popen)
    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_live)

    _enqueue_pane_state(app_env.db_path, "inst-stop", "@CC_STATE", "stopped")
    await main.process_pane_state_queue_once()

    assert popen_calls == [], "stopped must NOT spawn any close-down subprocess"


# ---- trg_tab_name_pane_state: rename enqueues @PANE_LABEL (trigger intact) ---


def test_rename_trigger_enqueues_pane_label(app_env: Any) -> None:
    """An UPDATE to ``instances.name`` fires trg_tab_name_pane_state → one @PANE_LABEL
    row with the raw new name. AFTER UPDATE, so the initial INSERT must NOT enqueue."""
    _insert_instance(app_env.db_path, "inst-rename", "old-name")
    assert _queue_rows(app_env.db_path, "@PANE_LABEL") == [], "INSERT must not enqueue"

    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute(
            "UPDATE instances SET name = ? WHERE id = ?",
            ("new-name", "inst-rename"),
        )
        conn.commit()

    assert _queue_rows(app_env.db_path, "@PANE_LABEL") == [
        ("inst-rename", "@PANE_LABEL", "new-name")
    ]


def test_rename_trigger_skips_noop(app_env: Any) -> None:
    """A rename to the SAME value (OLD IS NOT NEW is false) enqueues nothing."""
    _insert_instance(app_env.db_path, "inst-noop", "same-name")
    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute(
            "UPDATE instances SET name = ? WHERE id = ?",
            ("same-name", "inst-noop"),
        )
        conn.commit()
    assert _queue_rows(app_env.db_path, "@PANE_LABEL") == []
