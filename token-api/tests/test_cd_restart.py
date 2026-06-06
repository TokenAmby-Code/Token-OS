"""Tests for the CD restart-on-merge webhook.

POST /api/cd/restart — secret-validated (fail-closed), ack-first, restart-detached.
The webhook is now DUMB: every authorized merge spawns ONE detached, git-aware
`token-restart --sync` (which ff-pulls the live checkout and restarts only the
services the merge changed). There is no per-service routing in the handler
anymore — any legacy `services` field is accepted but informational. All actual
restarts (the detached spawn + save_restart_state) are monkeypatched, so these
tests exercise auth, the single-spawn contract, the pr_state→merged flip, and the
time-window coalescing guard without restarting anything.
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
    # Reset the time-window coalesce clock so each test starts uncoalesced
    # (module-level state otherwise leaks across tests in the same process).
    monkeypatch.setattr(app_env.main, "_cd_last_restart_spawn", 0.0, raising=False)
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


def _self_restarts(spawned):
    return [cmd for name, cmd in spawned if name == "self-restart"]


# ── Auth / fail-closed ───────────────────────────────────────


def test_missing_server_secret_fails_closed(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.delenv("CD_RESTART_SECRET", raising=False)
    c = TestClient(app_env.main.app)
    resp = c.post("/api/cd/restart", json={"sha": "abc"}, headers=_auth())
    assert resp.status_code == 503


def test_bad_secret_rejected(client):
    resp = client.post("/api/cd/restart", json={"sha": "abc"}, headers=_auth("wrong"))
    assert resp.status_code == 401


def test_missing_bearer_rejected(client):
    resp = client.post("/api/cd/restart", json={"sha": "abc"})
    assert resp.status_code == 401


# ── Single git-aware token-restart spawn ─────────────────────


def test_merge_spawns_one_git_aware_token_restart(client, spawned):
    resp = client.post("/api/cd/restart", json={"sha": "deadbeef"}, headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "scheduled" in body["restart"]
    # Exactly ONE detached spawn: token-restart, WITH --sync (so the merge is
    # ff-pulled into the live checkout before the git-aware selective restart).
    restarts = _self_restarts(spawned)
    assert len(restarts) == 1
    assert any("token-restart" in part for part in restarts[0]), restarts[0]
    assert restarts[0][-1].rstrip().endswith("--sync")
    # No per-service side-spawns anymore (no discord kickstart / push-mobile / curl).
    assert len(spawned) == 1


def test_no_services_field_still_spawns(client, spawned):
    # The git-aware webhook does not need a services list — token-restart derives
    # the changed set from git. An empty body (just a sha) still deploys.
    resp = client.post("/api/cd/restart", json={"sha": "cafef00d"}, headers=_auth())
    assert resp.status_code == 200, resp.text
    assert len(_self_restarts(spawned)) == 1


def test_legacy_services_field_accepted_but_not_routed(client, spawned):
    # A pre-git-aware workflow body (with a services list) is accepted, but the
    # handler does NOT do per-service routing — it just spawns token-restart.
    resp = client.post(
        "/api/cd/restart",
        json={"services": ["discord-daemon", "mobile"], "sha": "1234"},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    # One token-restart, and crucially NO discord kickstart / push-mobile spawns.
    assert len(spawned) == 1
    assert spawned[0][0] == "self-restart"
    assert not any(name in ("discord-restart", "push-mobile") for name, _ in spawned)


# ── pr_state → merged flip ───────────────────────────────────


def test_merged_pr_flips_instance_badge(client):
    url = "https://github.com/owner/repo/pull/42"
    iid = _insert_instance_with_pr(url, "open")
    resp = client.post(
        "/api/cd/restart",
        json={"sha": "abc", "pr_url": url},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pr_merged_flips"] == 1
    assert _pr_state(iid) == "merged"


# ── Time-window coalescing ───────────────────────────────────


def test_concurrent_restart_coalesces(client, spawned):
    first = client.post("/api/cd/restart", json={"sha": "a"}, headers=_auth())
    second = client.post("/api/cd/restart", json={"sha": "b"}, headers=_auth())
    assert first.status_code == 200 and second.status_code == 200
    assert "scheduled" in first.json()["restart"]
    assert "coalesced" in second.json()["restart"]
    # only ONE token-restart spawned despite two webhooks in the same window
    assert len(_self_restarts(spawned)) == 1
