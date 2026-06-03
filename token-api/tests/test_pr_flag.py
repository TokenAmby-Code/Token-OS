"""Tests for the "agent has a PR open" flag (Phase 1 of the CI/CD overhaul).

Covers:
- Schema: pr_url + pr_state columns on claude_instances (additive, nullable).
- Registration: pr_url + pr_state are sanctioned mutable fields.
- API: PATCH /api/instances/{id}/pr sets the flag; validation; 404 on unknown.
- Surface: /api/ui/ops/state carries pr_url/pr_state per active instance.

Uses a temporary SQLite database via TOKEN_API_DB env var (see conftest.app_env).
"""

import sqlite3
import uuid
from datetime import datetime

import pytest

_TEST_DB_PATH = None


@pytest.fixture
def client(app_env):
    from fastapi.testclient import TestClient

    return TestClient(app_env.main.app)


@pytest.fixture(autouse=True)
def _bind_globals(app_env):
    global _TEST_DB_PATH
    _TEST_DB_PATH = str(app_env.db_path)


def _insert_instance(instance_id=None, *, status="idle", working_dir="/tmp"):
    iid = instance_id or str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(_TEST_DB_PATH)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', ?, ?, ?)""",
        (iid, str(uuid.uuid4()), f"test-{iid[:8]}", working_dir, status, now, now),
    )
    conn.commit()
    conn.close()
    return iid


def _get_instance(instance_id):
    conn = sqlite3.connect(_TEST_DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM claude_instances WHERE id = ?", (instance_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Schema ───────────────────────────────────────────────────


def test_pr_columns_exist():
    conn = sqlite3.connect(_TEST_DB_PATH)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(claude_instances)")}
    conn.close()
    assert "pr_url" in cols
    assert "pr_state" in cols


def test_pr_columns_default_null():
    iid = _insert_instance()
    row = _get_instance(iid)
    assert row["pr_url"] is None
    assert row["pr_state"] is None


# ── Registration ─────────────────────────────────────────────


def test_pr_fields_are_sanctioned(app_env):
    import instance_mutation

    assert "pr_url" in instance_mutation.INSTANCE_MUTATION_FIELDS
    assert "pr_state" in instance_mutation.INSTANCE_MUTATION_FIELDS


# ── API ──────────────────────────────────────────────────────


def test_patch_pr_sets_open_flag(client):
    iid = _insert_instance()
    url = "https://github.com/owner/repo/pull/123"
    resp = client.patch(f"/api/instances/{iid}/pr", json={"pr_url": url, "pr_state": "open"})
    assert resp.status_code == 200, resp.text
    row = _get_instance(iid)
    assert row["pr_url"] == url
    assert row["pr_state"] == "open"


def test_patch_pr_flip_to_merged(client):
    iid = _insert_instance()
    url = "https://github.com/owner/repo/pull/7"
    client.patch(f"/api/instances/{iid}/pr", json={"pr_url": url, "pr_state": "open"})
    resp = client.patch(f"/api/instances/{iid}/pr", json={"pr_state": "merged"})
    assert resp.status_code == 200, resp.text
    row = _get_instance(iid)
    assert row["pr_state"] == "merged"
    assert row["pr_url"] == url  # untouched


def test_patch_pr_rejects_bad_state(client):
    iid = _insert_instance()
    resp = client.patch(f"/api/instances/{iid}/pr", json={"pr_state": "bogus"})
    assert resp.status_code == 400


def test_patch_pr_requires_a_field(client):
    iid = _insert_instance()
    resp = client.patch(f"/api/instances/{iid}/pr", json={})
    assert resp.status_code == 400


def test_patch_pr_unknown_instance_404(client):
    resp = client.patch("/api/instances/does-not-exist/pr", json={"pr_state": "open"})
    assert resp.status_code == 404


# ── Surface (/ui/ops) ────────────────────────────────────────


def test_ops_state_carries_pr_fields(client):
    iid = _insert_instance(status="processing")
    url = "https://github.com/owner/repo/pull/99"
    client.patch(f"/api/instances/{iid}/pr", json={"pr_url": url, "pr_state": "open"})

    resp = client.get("/api/ui/ops/state")
    assert resp.status_code == 200, resp.text
    active = resp.json()["instances"]["active"]
    mine = next((i for i in active if i["id"] == iid), None)
    assert mine is not None, "inserted instance not present in ops state"
    assert mine["pr_url"] == url
    assert mine["pr_state"] == "open"
