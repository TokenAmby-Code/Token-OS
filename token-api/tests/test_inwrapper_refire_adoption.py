"""In-wrapper SessionStart re-fire must adopt the existing row, never mint.

Falsified #198 (2026-06-13, %19/palace:S): driving a live claude through
``/plan`` enter→accept produced — under ONE wrapper launch — a fresh row + a
new ``needs-name`` session doc, retired the prior row, and left the pane's
``@INSTANCE_ID`` stamp empty. That is the exact continuity break #198's
stamp-reuse was meant to close.

Root cause is a race, not a wrong handler. Plan-accept fires ``SessionEnd``
then ``SessionStart`` in the SAME wrapper:

  1. ``handle_session_end`` unconditionally stops the row + spawns the tmuxctl
     assertion, which (registry row now ``stopped`` → assert ``ok=False``)
     clears ``@INSTANCE_ID``.
  2. The paired ``SessionStart`` arrives with a fresh ``session_id`` but the
     stamp is already gone, so #198's rescue has nothing to read and the
     handler mints a new id + orphan doc.

Two layers under test:

  * **Layer 1** — a non-terminal ``SessionEnd`` (``reason`` in {clear, compact})
    short-circuits BEFORE the stop + assertion, so the live row + stamp survive
    and the clean #198 stamp path re-keys with no further change.
  * **Layer 2** — a stamp-independent backstop: a ``SessionStart`` carrying a
    known ``wrapper_launch_id`` adopts the existing row keyed on that id
    (re-key, one row, no zombie) even if the stamp is lost for any reason.

Companion to ``test_session_start_stamp_reuse.py`` (the #198 stamp path).
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys


def _insert(db_path, instance_id, *, pane=None, status="idle", wrapper_launch_id=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            profile_name, tts_voice, notification_sound, status, tmux_pane)
           VALUES (?, ?, ?, '/tmp', 'local', 'Mac-Mini', 'p', 'v', 's', ?, ?)""",
        (instance_id, f"{instance_id}-session", instance_id, status, pane),
    )
    if wrapper_launch_id is not None:
        # wrapper_launch_id lives on the real instances table, not the legacy view.
        conn.execute(
            "UPDATE instances SET wrapper_launch_id = ? WHERE id = ?",
            (wrapper_launch_id, instance_id),
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
    """The race aftermath: no live pane resolution, no surviving stamp."""

    async def no_label(_pane):
        return None

    async def no_stamp(_pane):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)
    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", no_stamp)


def _spy_assertion(hooks, monkeypatch):
    """Record (never run) the tmuxctl SessionEnd assertion — the stamp-clearer."""
    calls = []

    def rec(pane, session_id):
        calls.append((pane, session_id))

    monkeypatch.setattr(hooks, "_spawn_session_end_assertion", rec)
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: None)
    return calls


def _start(hooks, payload):
    return asyncio.run(hooks.handle_session_start(payload))


def _end(hooks, payload):
    return asyncio.run(hooks.handle_session_end(payload))


# --------------------------------------------------------------------------- #
# Layer 2 — stamp-independent wrapper_launch_id adoption                       #
# --------------------------------------------------------------------------- #


def test_wrapper_launch_id_adopts_prior_row_when_stamp_lost(app_env, monkeypatch):
    """No stamp, no pane_instance_id — but the same wrapper_launch_id survives.

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
            # no pane_instance_id — the stamp was torn down by the race
            "env": {"TMUX_PANE": "%19", "TOKEN_API_ENGINE": "claude"},
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
            "env": {"TMUX_PANE": "%19", "TOKEN_API_ENGINE": "claude"},
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
            "env": {"TMUX_PANE": "%19", "TOKEN_API_ENGINE": "claude"},
        },
    )

    ids = _ids(app_env.db_path)
    assert "noid-new" in ids and "noid-old" in ids, f"blank id must not adopt: {ids}"


# --------------------------------------------------------------------------- #
# Layer 1 — non-terminal SessionEnd preserves the row + stamp                  #
# --------------------------------------------------------------------------- #


def test_clear_session_end_preserves_row_and_skips_assertion(app_env, monkeypatch):
    """reason='clear' is non-terminal: the row stays live and the stamp-clearing
    assertion is NOT spawned. RED today: the row goes stopped + assertion fires.
    """
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "clr", pane="%19", status="working")
    calls = _spy_assertion(hooks, monkeypatch)

    _end(hooks, {"session_id": "clr", "reason": "clear"})

    assert _status(app_env.db_path, "clr") != "stopped", (
        "non-terminal clear must not stop the row (stamp/continuity must survive)"
    )
    assert calls == [], f"non-terminal clear must not spawn the assertion: {calls}"


def test_compact_session_end_preserves_row_and_skips_assertion(app_env, monkeypatch):
    """reason='compact' is the other in-wrapper boundary — same treatment."""
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "cmp", pane="%19", status="working")
    calls = _spy_assertion(hooks, monkeypatch)

    _end(hooks, {"session_id": "cmp", "reason": "compact"})

    assert _status(app_env.db_path, "cmp") != "stopped"
    assert calls == [], f"non-terminal compact must not spawn the assertion: {calls}"


# --------------------------------------------------------------------------- #
# Regression guard — a terminal SessionEnd still tears down                    #
# --------------------------------------------------------------------------- #


def test_terminal_session_end_still_stops_and_asserts(app_env, monkeypatch):
    """A terminal reason (logout) keeps today's full teardown: row stopped +
    assertion spawned. The non-terminal allow-list must not over-reach.
    """
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "bye", pane="%19", status="working")
    calls = _spy_assertion(hooks, monkeypatch)

    _end(hooks, {"session_id": "bye", "reason": "logout"})

    assert _status(app_env.db_path, "bye") == "stopped", "terminal end must stop the row"
    assert calls, "terminal end must still spawn the SessionEnd assertion"


def test_missing_reason_session_end_still_stops(app_env, monkeypatch):
    """No reason field (legacy / unknown) defaults to the full terminal teardown."""
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "noreason", pane="%19", status="working")
    calls = _spy_assertion(hooks, monkeypatch)

    _end(hooks, {"session_id": "noreason"})

    assert _status(app_env.db_path, "noreason") == "stopped"
    assert calls, "unknown reason must keep today's teardown"
