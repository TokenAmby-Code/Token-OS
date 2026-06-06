"""Tests for the CD restart-on-merge webhook (Phase 2).

POST /api/cd/restart — secret-validated (fail-closed), ack-first, restart-detached.
All actual restarts (detached spawns + save_restart_state) are monkeypatched, so
these tests exercise auth, the service→action map, ack-first behavior, the
pr_state→merged flip, and the in-flight coalescing guard without restarting anything.
"""

import sqlite3
import uuid
from datetime import datetime

import pytest

_TEST_DB_PATH = None
SECRET = "test-cd-secret-abc123"


@pytest.fixture
def spawned():
    return []


@pytest.fixture
def client(app_env, monkeypatch, spawned):
    from fastapi.testclient import TestClient

    global _TEST_DB_PATH
    _TEST_DB_PATH = str(app_env.db_path)

    # Never actually restart anything.
    def _fake_spawn(cmd, *, log_name):
        spawned.append((log_name, list(cmd)))

    monkeypatch.setattr(app_env.main, "_cd_spawn_detached", _fake_spawn)
    monkeypatch.setattr(app_env.main, "save_restart_state", lambda: None)
    monkeypatch.setattr(app_env.main, "_cd_self_restart_scheduled", False, raising=False)
    monkeypatch.setenv("CD_RESTART_SECRET", SECRET)
    return TestClient(app_env.main.app)


def _auth(secret=SECRET):
    return {"Authorization": f"Bearer {secret}"}


def _insert_instance_with_pr(pr_url, pr_state="open"):
    iid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(_TEST_DB_PATH)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, registered_at, last_activity, pr_url, pr_state)
           VALUES (?, ?, ?, '/tmp', 'local', 'Mac-Mini', 'idle', ?, ?, ?, ?)""",
        (iid, str(uuid.uuid4()), f"t-{iid[:8]}", now, now, pr_url, pr_state),
    )
    conn.commit()
    conn.close()
    return iid


def _pr_state(iid):
    conn = sqlite3.connect(_TEST_DB_PATH)
    row = conn.execute("SELECT pr_state FROM claude_instances WHERE id = ?", (iid,)).fetchone()
    conn.close()
    return row[0] if row else None


# ── Auth / fail-closed ───────────────────────────────────────


def test_missing_server_secret_fails_closed(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.delenv("CD_RESTART_SECRET", raising=False)
    c = TestClient(app_env.main.app)
    resp = c.post("/api/cd/restart", json={"services": ["token-api"]}, headers=_auth())
    assert resp.status_code == 503


def test_bad_secret_rejected(client):
    resp = client.post("/api/cd/restart", json={"services": ["token-api"]}, headers=_auth("wrong"))
    assert resp.status_code == 401


def test_missing_bearer_rejected(client):
    resp = client.post("/api/cd/restart", json={"services": ["token-api"]})
    assert resp.status_code == 401


# ── Service → action map ─────────────────────────────────────


def test_token_api_schedules_self_restart(client, spawned):
    resp = client.post("/api/cd/restart", json={"services": ["token-api"]}, headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "scheduled" in body["self_restart"]
    # token-restart spawned detached, WITH --sync so the merge is ff-only pulled
    # into the live checkout before the restart (otherwise the merge ships nothing).
    self_restarts = [cmd for name, cmd in spawned if name == "self-restart"]
    assert len(self_restarts) == 1
    assert any("--sync" in part for part in self_restarts[0]), self_restarts[0]
    # ...specifically appended to the token-restart invocation, not loose.
    assert self_restarts[0][-1].rstrip().endswith("--sync")


def test_discord_only_does_not_self_restart(client, spawned):
    resp = client.post("/api/cd/restart", json={"services": ["discord-daemon"]}, headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["self_restart"] == "not requested"
    assert body["services"]["discord-daemon"] == "kicked"
    # a launchctl kickstart was spawned, no self-restart
    assert any(name == "discord-restart" for name, _ in spawned)
    assert not any(name == "self-restart" for name, _ in spawned)


def test_mobile_maps_to_push_mobile(client, spawned):
    resp = client.post("/api/cd/restart", json={"services": ["mobile"]}, headers=_auth())
    assert resp.status_code == 200
    assert any(name == "push-mobile" for name, _ in spawned)


def test_log_only_services_take_no_action(client, spawned):
    resp = client.post(
        "/api/cd/restart", json={"services": ["tmux", "ahk", "hammerspoon"]}, headers=_auth()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["self_restart"] == "not requested"
    assert all(v == "log-only (dropped)" for v in body["services"].values())
    assert spawned == []


# ── pr_state → merged flip ───────────────────────────────────


def test_merged_pr_flips_instance_badge(client):
    url = "https://github.com/owner/repo/pull/42"
    iid = _insert_instance_with_pr(url, "open")
    resp = client.post(
        "/api/cd/restart",
        json={"services": ["token-api"], "pr_url": url},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pr_merged_flips"] == 1
    assert _pr_state(iid) == "merged"


# ── In-flight coalescing ─────────────────────────────────────


def test_concurrent_self_restart_coalesces(client, spawned):
    first = client.post("/api/cd/restart", json={"services": ["token-api"]}, headers=_auth())
    second = client.post("/api/cd/restart", json={"services": ["token-api"]}, headers=_auth())
    assert first.status_code == 200 and second.status_code == 200
    assert "scheduled" in first.json()["self_restart"]
    assert "coalesced" in second.json()["self_restart"]
    # only ONE self-restart spawned despite two webhooks
    assert sum(1 for name, _ in spawned if name == "self-restart") == 1
