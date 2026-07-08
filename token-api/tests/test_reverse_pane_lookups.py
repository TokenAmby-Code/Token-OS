"""Slice B of the tmuxctl pane-ownership cutover: every ``pane -> instance``
reverse lookup in main.py reads the pane's live ``@INSTANCE_ID`` stamp instead of
querying the stored ``legacy_instances.tmux_pane`` column.

tmuxctl/the wrapper own the pane stamp (set at register, cleared on agent death),
so it is the authoritative reverse bridge. ``shared.instance_id_for_pane(pane)`` is
the one helper that reads it; it fails closed (``None``) on any miss/error.

Each site test inserts its row with a deliberately *non-matching* stored
``tmux_pane`` and resolves the row only through a stubbed ``instance_id_for_pane``
— so the test is RED against the old ``WHERE tmux_pane = ?`` code and GREEN only
once the site reads the stamp.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest


def _insert_instance(
    db_path: Any,
    instance_id: str,
    *,
    status: str = "idle",
    legion: str = "astartes",
    tab_name: str = "Worker",
    session_doc_id: int | None = None,
    hook_driven: int = 0,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id, status,
            instance_type, engine, legion, session_doc_id, hook_driven,
            zealotry)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            instance_id,
            tab_name,
            "/tmp",
            "local",
            "Mac-Mini",
            status,
            "sync",
            "claude",
            legion,
            session_doc_id,
            hook_driven,
            10,
        ),
    )
    conn.commit()
    conn.close()


_FETCH_QUERIES = {
    "tab_name": "SELECT tab_name FROM legacy_instances WHERE id = ?",
    "hook_driven": "SELECT hook_driven FROM legacy_instances WHERE id = ?",
    "status": "SELECT status FROM legacy_instances WHERE id = ?",
}


def _fetch(db_path: Any, instance_id: str, column: str) -> Any:
    # Allowlist the column to a fixed query (no identifier interpolation) so the
    # helper can never build dynamic SQL even if call sites grow.
    query = _FETCH_QUERIES[column]
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(query, (instance_id,)).fetchone()
    return row[0] if row else None


# ============================================================================
# The reusable primitive: shared.instance_id_for_pane
# ============================================================================


async def test_instance_id_for_pane_reads_live_stamp(app_env: Any, monkeypatch: Any) -> None:
    shared = app_env.shared

    captured: dict[str, Any] = {}

    async def _fake_offloop(cmd, **kwargs):
        captured["cmd"] = tuple(cmd)
        return SimpleNamespace(returncode=0, stdout=b"inst-live-42\n", stderr=b"")

    monkeypatch.setattr(shared, "_run_subprocess_offloop", _fake_offloop)

    got = await shared.instance_id_for_pane("%9")

    assert got == "inst-live-42"
    # Must read the pane's @INSTANCE_ID option, never the DB.
    assert captured["cmd"][:2] == ("tmux", "show-options")
    assert "@INSTANCE_ID" in captured["cmd"]
    assert "%9" in captured["cmd"]


async def test_instance_id_for_pane_unstamped_is_none(app_env: Any, monkeypatch: Any) -> None:
    shared = app_env.shared

    async def _empty(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(shared, "_run_subprocess_offloop", _empty)
    assert await shared.instance_id_for_pane("%9") is None


async def test_instance_id_for_pane_error_is_none(app_env: Any, monkeypatch: Any) -> None:
    shared = app_env.shared

    async def _fail(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"no such pane")

    monkeypatch.setattr(shared, "_run_subprocess_offloop", _fail)
    assert await shared.instance_id_for_pane("%9") is None


async def test_instance_id_for_pane_blank_pane_is_none(app_env: Any) -> None:
    shared = app_env.shared
    assert await shared.instance_id_for_pane("") is None
    assert await shared.instance_id_for_pane(None) is None


# ============================================================================
# Site: rename_instance_by_pane  (/api/instance/rename)
# ============================================================================


async def test_rename_by_pane_resolves_via_stamp(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    # Stored pane intentionally != the live pane the agent reports.
    _insert_instance(app_env.db_path, "inst-A", tab_name="Old")

    async def _stamp(pane):
        return "inst-A" if pane == "%LIVE" else None

    monkeypatch.setattr(main.shared, "instance_id_for_pane", _stamp)

    result = await main.rename_instance_by_pane(
        main.PaneRenameRequest(tmux_pane="%LIVE", tab_name="Fresh Name")
    )

    assert result["status"] == "renamed"
    assert result["instance_id"] == "inst-A"
    assert _fetch(app_env.db_path, "inst-A", "tab_name") == "Fresh Name"


async def test_rename_by_pane_404_when_pane_unstamped(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "inst-A", tab_name="Old")

    async def _no_stamp(_pane):
        return None

    monkeypatch.setattr(main.shared, "instance_id_for_pane", _no_stamp)

    with pytest.raises(main.HTTPException) as exc:
        await main.rename_instance_by_pane(
            main.PaneRenameRequest(tmux_pane="%LIVE", tab_name="Fresh")
        )
    assert exc.value.status_code == 404
    # Stored column must not be consulted as a fallback.
    assert _fetch(app_env.db_path, "inst-A", "tab_name") == "Old"


# ============================================================================
# Site: _flag_hook_driven  (pane fallback)
# ============================================================================


async def test_flag_hook_driven_pane_fallback_uses_stamp(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "inst-B", hook_driven=0)

    async def _stamp(pane):
        return "inst-B" if pane == "%LIVE" else None

    monkeypatch.setattr(main.shared, "instance_id_for_pane", _stamp)

    await main._flag_hook_driven(tmux_pane="%LIVE", actor="test")

    assert _fetch(app_env.db_path, "inst-B", "hook_driven") == 1


async def test_flag_hook_driven_prefers_explicit_id(app_env: Any, monkeypatch: Any) -> None:
    """The id path is unchanged: an explicit instance_id still wins, no stamp read."""
    main = app_env.main
    _insert_instance(app_env.db_path, "inst-B", hook_driven=0)

    called: list[Any] = []

    async def _stamp(pane):
        called.append(pane)
        return None

    monkeypatch.setattr(main.shared, "instance_id_for_pane", _stamp)

    await main._flag_hook_driven(instance_id="inst-B", actor="test")

    assert _fetch(app_env.db_path, "inst-B", "hook_driven") == 1
    assert called == [], "stamp must not be read when an explicit id resolves"


# ============================================================================
# Site: pane_instance + pane_session_doc routes
# ============================================================================


async def test_pane_instance_route_resolves_via_stamp(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "inst-C", status="idle")

    async def _stamp(pane):
        return "inst-C" if pane == "%LIVE" else None

    monkeypatch.setattr(main.shared, "instance_id_for_pane", _stamp)

    inst = await main.pane_instance("%LIVE")
    assert inst["id"] == "inst-C"


async def test_pane_instance_route_404_when_unstamped(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "inst-C", status="idle")

    async def _no_stamp(_pane):
        return None

    monkeypatch.setattr(main.shared, "instance_id_for_pane", _no_stamp)

    with pytest.raises(main.HTTPException) as exc:
        await main.pane_instance("%LIVE")
    assert exc.value.status_code == 404


async def test_pane_session_doc_route_resolves_via_stamp(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute(
            "INSERT INTO session_documents (id, title, file_path, project) VALUES (?, ?, ?, ?)",
            (501, "Doc", "/Volumes/Imperium/Imperium-ENV/Mars/Sessions/x.md", "mars"),
        )
        conn.commit()
    _insert_instance(app_env.db_path, "inst-G", session_doc_id=501)

    async def _stamp(pane):
        return "inst-G" if pane == "%LIVE" else None

    monkeypatch.setattr(main.shared, "instance_id_for_pane", _stamp)

    out = await main.pane_session_doc("%LIVE")
    assert out["instance_id"] == "inst-G"
    assert out["doc_id"] == 501


async def test_pane_session_doc_404_for_stopped_instance(app_env: Any, monkeypatch: Any) -> None:
    """During the stop->stamp-clear race a stopped instance can still resolve; the
    route filters status != 'stopped' to stay consistent with pane_instance."""
    main = app_env.main
    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute(
            "INSERT INTO session_documents (id, title, file_path, project) VALUES (?, ?, ?, ?)",
            (502, "Doc", "/Volumes/Imperium/Imperium-ENV/Mars/Sessions/y.md", "mars"),
        )
        conn.commit()
    _insert_instance(app_env.db_path, "inst-Gs", session_doc_id=502, status="stopped")

    async def _stamp(pane):
        return "inst-Gs" if pane == "%LIVE" else None

    monkeypatch.setattr(main.shared, "instance_id_for_pane", _stamp)

    with pytest.raises(main.HTTPException) as exc:
        await main.pane_session_doc("%LIVE")
    assert exc.value.status_code == 404


# ============================================================================
# Site: _pane_sender_is_custodes
# ============================================================================


async def test_pane_sender_is_custodes_via_stamp(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "inst-D", legion="custodes")

    async def _stamp(pane):
        return "inst-D" if pane == "%LIVE" else None

    monkeypatch.setattr(main.shared, "instance_id_for_pane", _stamp)

    assert await main._pane_sender_is_custodes("%LIVE") is True


async def test_pane_sender_non_custodes_via_stamp(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "inst-D2", legion="mechanicus")

    async def _stamp(pane):
        return "inst-D2" if pane == "%LIVE" else None

    monkeypatch.setattr(main.shared, "instance_id_for_pane", _stamp)

    assert await main._pane_sender_is_custodes("%LIVE") is False


# ============================================================================
# Site: _inject_custodes_via_singleton_pane
# ============================================================================


async def test_inject_custodes_singleton_resolves_id_via_stamp(
    app_env: Any, monkeypatch: Any
) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "inst-E", legion="custodes")

    async def _find_pane():
        return "%LIVE"

    async def _stamp(pane):
        return "inst-E" if pane == "%LIVE" else None

    seen: dict[str, Any] = {}

    async def _inject(legion, instance_id, tmux_pane, formatted, channel_name):
        seen["legion"] = legion
        seen["instance_id"] = instance_id
        seen["tmux_pane"] = tmux_pane
        return True

    monkeypatch.setattr(main, "_find_custodes_tmux_pane", _find_pane)
    monkeypatch.setattr(main.shared, "instance_id_for_pane", _stamp)
    monkeypatch.setattr(main, "_agent_cmd_inject", _inject)

    ok = await main._inject_custodes_via_singleton_pane("hello", "custodes-voice")

    assert ok is True
    assert seen["instance_id"] == "inst-E", "instance_id must come from the pane stamp"
    assert seen["tmux_pane"] == "%LIVE"


# ============================================================================
# Site: stop_instance does not own tmux pane chrome teardown
# ============================================================================


async def test_stop_instance_does_not_stamp_gate_or_clear_pane_tint(
    app_env: Any, monkeypatch: Any
) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "inst-F", legion="custodes")

    async def _resolve(_instance_id):
        raise AssertionError("stop_instance must not key chrome clear on @INSTANCE_ID")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve)

    cleared: list[Any] = []
    monkeypatch.setattr(main.shared, "clear_pane_tint", lambda pane, **kw: cleared.append(pane))

    async def _noop_widget(*a, **k):
        return None

    monkeypatch.setattr(main, "push_phone_widget_async", _noop_widget)

    await main.stop_instance("inst-F")

    assert _fetch(app_env.db_path, "inst-F", "status") == "stopped"
    assert cleared == [], "tmuxctld atomic teardown owns pane chrome clear"


async def test_stop_instance_no_tint_clear_when_pane_gone(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "inst-H", legion="custodes")

    async def _gone(_instance_id):
        return (None, None)

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _gone)

    cleared: list[Any] = []
    monkeypatch.setattr(main.shared, "clear_pane_tint", lambda pane, **kw: cleared.append(pane))

    async def _noop_widget(*a, **k):
        return None

    monkeypatch.setattr(main, "push_phone_widget_async", _noop_widget)

    await main.stop_instance("inst-H")

    assert _fetch(app_env.db_path, "inst-H", "status") == "stopped"
    assert cleared == [], "a vanished pane must not be tint-cleared"
