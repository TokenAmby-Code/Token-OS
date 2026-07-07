"""SessionStart pane-occupant adoption is live-oracle only.

Token-API no longer accepts client-shipped pane-stamp compatibility payloads.
The only pane-occupancy source in SessionStart is tmuxctld's live read-only
oracle; wrapper-launch adoption is the separate in-wrapper continuity backstop.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import uuid

from fastapi.testclient import TestClient


def _insert(db_path, instance_id, *, status="idle", wrapper_launch_id=None):
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
    rows = conn.execute("SELECT id, status FROM instances ORDER BY id").fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


def _start(hooks, payload):
    return asyncio.run(hooks.handle_session_start(payload))


def test_live_tmuxctl_occupant_adopts_prior_row(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "ctx-old", status="working", wrapper_launch_id="wrap-live")

    async def no_label(_pane):
        return None

    async def live_occupant(pane):
        return "ctx-old" if pane == "%77" else None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)
    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", live_occupant)

    _start(
        hooks,
        {
            "session_id": "ctx-new",
            "cwd": "/tmp",
            "pid": 4242,
            "wrapper_launch_id": "wrap-live",
            "env": {"TMUX_PANE": "%77", "TOKEN_API_ENGINE": "claude"},
        },
    )

    ids = _ids(app_env.db_path)
    assert ids == {"ctx-new": "idle"}


def test_blind_tmuxctl_without_wrapper_identity_registers_fresh(app_env, monkeypatch):
    """A live-oracle miss is not rescued by any Token-API payload shim."""
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "ctx-old", status="working")

    async def no_label(_pane):
        return None

    async def no_occupant(_pane):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)
    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", no_occupant)

    legacy_payload_key = "pane_" + "instance_id"
    _start(
        hooks,
        {
            "session_id": "ctx-new",
            "cwd": "/tmp",
            "pid": 4242,
            legacy_payload_key: "ctx-old",
            "env": {"TMUX_PANE": "%77", "TOKEN_API_ENGINE": "claude"},
        },
    )

    ids = _ids(app_env.db_path)
    assert ids == {"ctx-new": "idle", "ctx-old": "working"}


def test_register_endpoint_is_idempotent_on_known_id(app_env, monkeypatch):
    """POST /api/instances/register twice with one id -> one row, two 200s."""

    async def _noop_push(*args, **kwargs):
        return None

    monkeypatch.setattr(app_env.main, "push_phone_widget_async", _noop_push)
    client = TestClient(app_env.main.app)
    instance_id = str(uuid.uuid4())
    body = {"instance_id": instance_id, "name": "inst", "working_dir": "/tmp/inst"}

    first = client.post("/api/instances/register", json=body)
    assert first.status_code == 200, first.text

    second = client.post("/api/instances/register", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["profile"]["name"], "re-register must still return a profile"

    assert list(_ids(app_env.db_path)) == [instance_id]
