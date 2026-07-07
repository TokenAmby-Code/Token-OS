"""In-wrapper SessionStart re-fire must adopt the existing row, never mint.

Falsified #198 (2026-06-13, %19/palace:S): driving a live claude through
``/plan`` enter→accept produced — under ONE wrapper launch — a fresh row + a
new ``needs-name`` session doc, retired the prior row, and left no pane-local identity for tmuxctld to reconcile. That is the
continuity break the wrapper_launch_id backstop closes.

Root cause is a race, not a wrong handler. Plan-accept fires ``SessionEnd``
then ``SessionStart`` in the SAME wrapper:

  1. ``handle_session_end`` unconditionally stops the row (and, historically,
     spawned the tmuxctl assert-instance COLD path, which on a now-``stopped``
     row asserted ``ok=False`` and cleared ``@INSTANCE_ID``). That COLD path has
     since been severed entirely (boundary doctrine: SessionEnd is instance-level
     and never reaches across to invoke tmuxctl pane-control) — but marking the
     row ``stopped`` alone still races the re-fire and drops it out of active
     state mid-session.
  2. The paired ``SessionStart`` arrives with a fresh ``session_id`` but the
     row is gone/stopped, so #198's rescue has nothing to read and the
     handler mints a new id + orphan doc.

Two layers under test:

  * **Layer 1** — a non-terminal ``SessionEnd`` (``reason`` in {clear, compact})
    short-circuits BEFORE the stop, so the live row survives for the paired
    SessionStart. As a boundary guard it also confirms no tmuxctl pane-control is
    invoked. Pane-local stamp continuity is tmuxctld/wrapper-owned.
  * **Layer 2** — a stamp-independent backstop: a ``SessionStart`` carrying a
    known ``wrapper_launch_id`` adopts the existing row keyed on that id
    (re-key, one row, no zombie) even if the stamp is lost for any reason.

Companion to the read-only pane-stamp adoption tests.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys

# A pane id that does NOT exist on any live tmux server. The hooks' tmux-WRITE
# paths are also mocked (_mute_tmux_writes), but a fake pane is a second guard:
# an early version used the real "%19" and clobbered that live pane's
# @INSTANCE_ID. Never put a real pane id in a hook test.
_FAKE_PANE = "%999019"


def _insert(db_path, instance_id, *, pane=None, status="idle", wrapper_launch_id=None):
    # Pane liveness is no longer a stored column; ``pane`` is vestigial test data.
    conn = sqlite3.connect(db_path)
    persona_id = conn.execute("SELECT id FROM personas WHERE slug='blood-angels'").fetchone()[0]
    conn.execute(
        """INSERT INTO instances
             (id, name, working_dir, origin_type, device_id, persona_id, rank,
              status, wrapper_launch_id, last_activity)
           VALUES (?, ?, '/tmp', 'local', 'Mac-Mini', ?, 'astartes', ?, ?,
                   '2026-07-01T00:00:00')""",
        (instance_id, instance_id, persona_id, status, wrapper_launch_id),
    )
    conn.commit()
    conn.close()


def _ids(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id, status FROM instances").fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


def _status(db_path, instance_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status FROM instances WHERE id = ?", (instance_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def _blind_tmuxctl(hooks, monkeypatch):
    """The race aftermath: no live pane resolution, no payload stamp."""

    async def no_label(_pane):
        return None

    async def no_stamp(_pane):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)
    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", no_stamp)
    _mute_tmux_writes(hooks, monkeypatch)


def _mute_tmux_writes(hooks, monkeypatch):
    """Neutralize tint writes; these tests assert on DB rows only."""

    async def _astamp(*_args, **_kwargs):
        return None

    def _sync_noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(hooks.shared, "clear_pane_tint", _sync_noop)
    monkeypatch.setattr(hooks.shared, "apply_instance_pane_tint", _astamp)


def _spy_tmuxctl(hooks, monkeypatch):
    """Record any tmuxctl / assert-instance subprocess SessionEnd might spawn.

    The SessionEnd → tmuxctl assert-instance COLD path was severed (it reached
    from an instance-level hook into tmuxctld's pane domain and culled live
    panes during plan-mode context-clears). SessionEnd must now spawn NO tmuxctl
    call at all; this records the subprocess seam so a regression is caught. The
    stop_hook spawn (non-tmuxctl argv) is filtered out and runs as a no-op.
    """
    calls = []

    def _spy_popen(args, *_a, **_k):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if any("assert-instance" in str(x) or str(x).endswith("tmuxctl") for x in argv):
            calls.append(tuple(str(x) for x in argv))
        return None

    monkeypatch.setattr(hooks.subprocess, "Popen", _spy_popen)
    _mute_tmux_writes(hooks, monkeypatch)
    return calls


def _start(hooks, payload):
    return asyncio.run(hooks.handle_session_start(payload))


def _end(hooks, payload):
    return asyncio.run(hooks.handle_session_end(payload))


# --------------------------------------------------------------------------- #
# Layer 2 — stamp-independent wrapper_launch_id adoption                       #
# --------------------------------------------------------------------------- #


def test_wrapper_launch_id_adopts_prior_row_when_stamp_lost(app_env, monkeypatch):
    """No pane stamp payload — but the same wrapper_launch_id survives.

    The SessionStart must adopt (re-key) the prior row, not mint a duplicate.
    RED today: with the stamp gone the handler falls through to the mint.
    """
    hooks = sys.modules["routes.hooks"]
    _insert(
        app_env.db_path,
        "wl-old",
        pane=None,
        status="working",
        wrapper_launch_id="LAUNCH-abc123",
    )
    _blind_tmuxctl(hooks, monkeypatch)

    _start(
        hooks,
        {
            "session_id": "wl-new",
            "cwd": "/tmp",
            "pid": 4242,
            "wrapper_launch_id": "LAUNCH-abc123",
            # no pane-stamp payload — the stamp was torn down by the race
            "env": {"TMUX_PANE": _FAKE_PANE, "TOKEN_API_ENGINE": "claude"},
        },
    )

    ids = _ids(app_env.db_path)
    assert "wl-new" in ids, f"adopted row should be re-keyed to the new id: {ids}"
    assert "wl-old" not in ids, f"prior row must be adopted, not a zombie: {ids}"
    assert len(ids) == 1, f"in-wrapper re-fire must not mint a duplicate row: {ids}"


def test_wrapper_launch_id_does_not_adopt_across_launches(app_env, monkeypatch):
    """A genuine close→reboot mints a fresh independent instance.

    A different wrapper_launch_id must NOT adopt the prior row — that would
    break the Emperor-confirmed independent-instance-on-reboot semantics.
    """
    hooks = sys.modules["routes.hooks"]
    _insert(
        app_env.db_path,
        "boot-old",
        pane=None,
        status="working",
        wrapper_launch_id="LAUNCH-first",
    )
    _blind_tmuxctl(hooks, monkeypatch)

    _start(
        hooks,
        {
            "session_id": "boot-new",
            "cwd": "/tmp",
            "pid": 5555,
            "wrapper_launch_id": "LAUNCH-second",
            "env": {"TMUX_PANE": _FAKE_PANE, "TOKEN_API_ENGINE": "claude"},
        },
    )

    ids = _ids(app_env.db_path)
    assert "boot-new" in ids, f"reboot must register a fresh row: {ids}"
    assert "boot-old" in ids, f"prior launch's row must be left alone: {ids}"


def test_wrapper_launch_id_blank_does_not_adopt(app_env, monkeypatch):
    """A missing wrapper_launch_id must never collapse unrelated rows together."""
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "noid-old", pane=None, status="working", wrapper_launch_id=None)
    _blind_tmuxctl(hooks, monkeypatch)

    _start(
        hooks,
        {
            "session_id": "noid-new",
            "cwd": "/tmp",
            "pid": 6666,
            # no wrapper_launch_id at all
            "env": {"TMUX_PANE": _FAKE_PANE, "TOKEN_API_ENGINE": "claude"},
        },
    )

    ids = _ids(app_env.db_path)
    assert "noid-new" in ids and "noid-old" in ids, f"blank id must not adopt: {ids}"


# --------------------------------------------------------------------------- #
# Layer 1 — non-terminal SessionEnd preserves the row                          #
# --------------------------------------------------------------------------- #


def test_clear_session_end_preserves_row_and_skips_tmuxctl(
    app_env: object, monkeypatch: object
) -> None:
    """reason='clear' is non-terminal: the row stays live and no tmuxctl
    pane-control is invoked. RED before #198: the row went stopped.
    """
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "clr", pane=_FAKE_PANE, status="working")
    calls = _spy_tmuxctl(hooks, monkeypatch)

    _end(hooks, {"session_id": "clr", "reason": "clear"})

    assert _status(app_env.db_path, "clr") != "stopped", (
        "non-terminal clear must not stop the row (continuity must survive)"
    )
    assert calls == [], f"non-terminal clear must not invoke tmuxctl: {calls}"


def test_compact_session_end_preserves_row_and_skips_tmuxctl(
    app_env: object, monkeypatch: object
) -> None:
    """reason='compact' is the other in-wrapper boundary — same treatment."""
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "cmp", pane=_FAKE_PANE, status="working")
    calls = _spy_tmuxctl(hooks, monkeypatch)

    _end(hooks, {"session_id": "cmp", "reason": "compact"})

    assert _status(app_env.db_path, "cmp") != "stopped"
    assert calls == [], f"non-terminal compact must not invoke tmuxctl: {calls}"


# --------------------------------------------------------------------------- #
# Boundary guard — a terminal SessionEnd stops the row but NEVER calls tmuxctl #
# --------------------------------------------------------------------------- #


def test_terminal_session_end_stops_row_without_tmuxctl(
    app_env: object, monkeypatch: object
) -> None:
    """A terminal reason (logout) stops the row (instance-domain) but invokes NO
    tmuxctl pane-control — the severed COLD path. The non-terminal allow-list
    must not over-reach: terminal ends still stop the row.
    """
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "bye", pane=_FAKE_PANE, status="working")
    calls = _spy_tmuxctl(hooks, monkeypatch)

    _end(hooks, {"session_id": "bye", "reason": "logout"})

    assert _status(app_env.db_path, "bye") == "stopped", "terminal end must stop the row"
    assert calls == [], f"terminal end must not invoke tmuxctl pane-control: {calls}"


def test_missing_reason_session_end_stops_row_without_tmuxctl(
    app_env: object, monkeypatch: object
) -> None:
    """No reason field (legacy / unknown) defaults to terminal teardown: row
    stopped, still no tmuxctl pane-control."""
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "noreason", pane=_FAKE_PANE, status="working")
    calls = _spy_tmuxctl(hooks, monkeypatch)

    _end(hooks, {"session_id": "noreason"})

    assert _status(app_env.db_path, "noreason") == "stopped"
    assert calls == [], f"unknown reason must not invoke tmuxctl pane-control: {calls}"
