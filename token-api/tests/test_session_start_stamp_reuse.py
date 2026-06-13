"""SessionStart stamp-reuse: the client-shipped @INSTANCE_ID payload field.

Root cause (2026-06-12 duplicate-custodes): plan-approval context-clear makes
Claude Code emit a fresh ``session_id`` while the process keeps its tmux pane.
The pane-occupant supplant (case 4) resolves occupancy only through a *live*
tmuxctl ``shared.instance_id_for_pane`` lookup at SessionStart time — any miss
(tmuxctl latency, SMB stall, hook racing the stamp) fails closed and a duplicate
row is INSERTed. The fresh row gets default identity while the prior row keeps
persona/rank, so persona+rank resolution (enforcement dispatch) routes to a
corpse.

Fix under test: generic-hook.sh reads the pane's own ``@INSTANCE_ID`` stamp and
ships it as payload ``pane_instance_id``; ``handle_session_start`` uses it as
the fallback occupancy source wherever it consults the live stamp. The bare
``POST /api/instances/register`` endpoint becomes idempotent on a known id.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import uuid

from fastapi.testclient import TestClient


def _insert(db_path, instance_id, *, pane=None, status="idle"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            profile_name, tts_voice, notification_sound, status, tmux_pane)
           VALUES (?, ?, ?, '/tmp', 'local', 'Mac-Mini', 'p', 'v', 's', ?, ?)""",
        (instance_id, f"{instance_id}-session", instance_id, status, pane),
    )
    conn.commit()
    conn.close()


def _ids(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id, status FROM instances").fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


def _blind_tmuxctl(hooks, monkeypatch):
    """The failure mode: live pane resolution misses entirely."""

    async def no_label(_pane):
        return None

    async def no_stamp(_pane):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)
    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", no_stamp)


def _start(hooks, payload):
    return asyncio.run(hooks.handle_session_start(payload))


def test_payload_stamp_adopts_prior_row_when_tmuxctl_blind(app_env, monkeypatch):
    """Context-clear: fresh session_id + payload stamp → adopt, never duplicate."""
    hooks = sys.modules["routes.hooks"]
    # No stored tmux_pane: the column is deprecated post-extraction, so the
    # legacy stored-pane fallback in case 4 cannot rescue this row. The payload
    # stamp is the only surviving occupancy signal.
    _insert(app_env.db_path, "ctx-old", pane=None, status="working")
    _blind_tmuxctl(hooks, monkeypatch)

    _start(
        hooks,
        {
            "session_id": "ctx-new",
            "cwd": "/tmp",
            "pid": 4242,
            "pane_instance_id": "ctx-old",
            "env": {"TMUX_PANE": "%77", "TOKEN_API_ENGINE": "claude"},
        },
    )

    ids = _ids(app_env.db_path)
    assert "ctx-new" in ids, f"adopted row should be re-keyed to the new session id: {ids}"
    assert "ctx-old" not in ids, f"prior row must be adopted, not left as a zombie: {ids}"
    assert len(ids) == 1, f"context-clear must not mint a duplicate row: {ids}"


def test_live_tmuxctl_stamp_outranks_payload_stamp(app_env, monkeypatch):
    """When tmuxctl *can* resolve the pane, its (fresher) answer wins."""
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "live-occupant", pane="%88", status="working")
    _insert(app_env.db_path, "stale-stamp", pane=None, status="working")

    async def no_label(_pane):
        return None

    async def live_stamp(pane):
        return "live-occupant" if pane == "%88" else None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)
    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", live_stamp)

    _start(
        hooks,
        {
            "session_id": "fresh-id",
            "cwd": "/tmp",
            "pid": 4242,
            "pane_instance_id": "stale-stamp",
            "env": {"TMUX_PANE": "%88", "TOKEN_API_ENGINE": "claude"},
        },
    )

    ids = _ids(app_env.db_path)
    assert "fresh-id" in ids and "live-occupant" not in ids
    assert "stale-stamp" in ids, "the stale payload stamp's row must be left alone"


def test_payload_stamp_matching_own_session_is_not_a_supplant(app_env, monkeypatch):
    """--continue re-registration: stamp == session_id is the same instance."""
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "same-id", pane="%55", status="idle")
    _blind_tmuxctl(hooks, monkeypatch)

    _start(
        hooks,
        {
            "session_id": "same-id",
            "cwd": "/tmp",
            "pid": 4242,
            "pane_instance_id": "same-id",
            "env": {"TMUX_PANE": "%55", "TOKEN_API_ENGINE": "claude"},
        },
    )

    ids = _ids(app_env.db_path)
    assert list(ids) == ["same-id"]


def test_payload_stamp_never_resurrects_stopped_row(app_env, monkeypatch):
    """A stamp left behind by a stopped instance must not be adopted."""
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "corpse", pane=None, status="stopped")
    _blind_tmuxctl(hooks, monkeypatch)

    _start(
        hooks,
        {
            "session_id": "newcomer",
            "cwd": "/tmp",
            "pid": 4242,
            "pane_instance_id": "corpse",
            "env": {"TMUX_PANE": "%66", "TOKEN_API_ENGINE": "claude"},
        },
    )

    ids = _ids(app_env.db_path)
    assert ids.get("corpse") == "stopped", "stopped row must stay stopped"
    assert "newcomer" in ids, "a fresh row is correct when the stamp points at a corpse"


def test_register_endpoint_is_idempotent_on_known_id(app_env, monkeypatch):
    """POST /api/instances/register twice with one id → one row, two 200s."""

    async def _noop_push(*args, **kwargs):
        return None

    monkeypatch.setattr(app_env.main, "push_phone_widget_async", _noop_push)
    client = TestClient(app_env.main.app)
    instance_id = str(uuid.uuid4())
    body = {"instance_id": instance_id, "tab_name": "inst", "working_dir": "/tmp/inst"}

    first = client.post("/api/instances/register", json=body)
    assert first.status_code == 200, first.text

    second = client.post("/api/instances/register", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["profile"]["name"], "re-register must still return a profile"

    assert list(_ids(app_env.db_path)) == [instance_id]
