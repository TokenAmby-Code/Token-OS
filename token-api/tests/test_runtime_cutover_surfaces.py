"""Regression pins: the runtime instance surfaces are cut off the dropped table.

`claude_instances` was extracted to archive.db and dropped (see
test_claude_instances_exterminatus.py). The exterminatus suite pins the schema,
SessionStart registration, and the low-level sanctioned writers. This suite pins
the three HTTP surfaces an agent actually drives end-to-end — the ones the
civic-keeper instance found broken against the stale live runtime:

  (a) rename  — POST /api/instance/rename (the instance-name CLI's pane-scoped
      route) and PATCH /api/instances/{id}/rename update the `instances` row
      `name`, and the API reflects it.
  (b) pane-bind — EXTERMINATED. Pane ids are no longer persisted; the tmuxctl
      @INSTANCE_ID oracle resolves pane geometry live. The "instances never stores
      tmux_pane/pane_label" invariant is pinned in test_claude_instances_exterminatus.
  (c) resolve — GET /api/instances/resolve and GET /api/panes/{pane}/instance
      return the live instance out of `instances`.

Every assertion also confirms the live DB never resurrects `claude_instances`.
"""

import sqlite3
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient


def _db(app_env: Any) -> sqlite3.Connection:
    """Open a row-factory connection on the test's isolated agents.db."""
    conn = sqlite3.connect(app_env.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    """Return the set of table names in the live (main) schema."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


@pytest.fixture
def client(app_env: Any) -> TestClient:
    """TestClient over the reloaded FastAPI app bound to the test DB."""
    return TestClient(app_env.main.app)


def _session_start(
    client: Any,
    *,
    tmux_pane: str | None = None,
    pane_label: str | None = None,
    cwd: str | None = None,
) -> str:
    """Drive a SessionStart hook and return the registered instance id."""
    instance_id = str(uuid.uuid4())
    payload = {
        "session_id": instance_id,
        "cwd": cwd or f"/tmp/{instance_id}",
        "pid": 12345,
    }
    if tmux_pane is not None:
        payload["tmux_pane"] = tmux_pane
    if pane_label is not None:
        payload["pane_label"] = pane_label
    resp = client.post("/api/hooks/SessionStart", json=payload)
    assert resp.status_code == 200, resp.text
    return instance_id


# ── (a) rename ───────────────────────────────────────────────────────────────


class TestRenameHitsInstances:
    def test_patch_rename_updates_instances_row(self, client, app_env):
        """PATCH /api/instances/{id}/rename writes name to instances and the API reflects it."""
        instance_id = _session_start(client)
        resp = client.patch(
            f"/api/instances/{instance_id}/rename", json={"tab_name": "civic-keeper"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["tab_name"] == "civic-keeper"

        conn = _db(app_env)
        row = conn.execute("SELECT name FROM instances WHERE id = ?", (instance_id,)).fetchone()
        names = _table_names(conn)
        conn.close()
        assert row is not None and row["name"] == "civic-keeper"
        assert "claude_instances" not in names

        # API reflects the new name.
        got = client.get(f"/api/instances/{instance_id}")
        assert got.status_code == 200, got.text
        assert got.json()["tab_name"] == "civic-keeper"

    def test_pane_scoped_rename_updates_instances_row(self, client, app_env, monkeypatch):
        """POST /api/instance/rename is what the `instance-name` CLI calls from a
        pane. The civic-keeper instance hit this route and had to fall back to an
        `--id` workaround; pin it onto `instances`.
        """
        instance_id = _session_start(client)

        async def _fake_instance_id_for_pane(pane: str) -> str:
            """Stub the live @INSTANCE_ID stamp lookup tmuxctl would do."""
            return instance_id

        monkeypatch.setattr(app_env.shared, "instance_id_for_pane", _fake_instance_id_for_pane)
        # main.py calls shared.instance_id_for_pane via its bound `shared` module.
        monkeypatch.setattr(app_env.main.shared, "instance_id_for_pane", _fake_instance_id_for_pane)

        resp = client.post(
            "/api/instance/rename",
            json={"tmux_pane": "%25", "tab_name": "civic-keeper"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["tab_name"] == "civic-keeper"
        assert resp.json()["instance_id"] == instance_id

        conn = _db(app_env)
        row = conn.execute("SELECT name FROM instances WHERE id = ?", (instance_id,)).fetchone()
        names = _table_names(conn)
        conn.close()
        assert row is not None and row["name"] == "civic-keeper"
        assert "claude_instances" not in names


# ── (b) pane-bind — EXTERMINATED ─────────────────────────────────────────────
#
# The original part (b) pinned the OPPOSITE of the current invariant: that a
# SessionStart pane-bind PERSISTS instances.tmux_pane/pane_label and GET
# /api/instances surfaces them. Pane ids are no longer stored — the tmuxctl
# @INSTANCE_ID oracle resolves pane geometry live — so those tests were deleted.
# The "instances never carries tmux_pane/pane_label" invariant is pinned by
# tests/test_claude_instances_exterminatus.py::test_instances_has_runtime_annex_columns.


# ── (c) resolve ──────────────────────────────────────────────────────────────


class TestResolveHitsInstances:
    def test_resolve_by_cwd_returns_instance(self, client, app_env):
        """GET /api/instances/resolve returns the live instance matched by cwd from instances."""
        cwd = f"/tmp/resolve-{uuid.uuid4()}"
        instance_id = _session_start(client, cwd=cwd)

        resp = client.get("/api/instances/resolve", params={"cwd": cwd})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == instance_id, "resolve did not return the live instance"

        conn = _db(app_env)
        names = _table_names(conn)
        conn.close()
        assert "claude_instances" not in names

    def test_pane_instance_lookup_returns_from_instances(self, client, app_env, monkeypatch):
        """GET /api/panes/{pane}/instance resolves the custodes-style `%25` pane to
        the live row out of `instances` (the "%25 not resolving" surface)."""
        instance_id = _session_start(client, tmux_pane="%25", pane_label="palace:NW")

        async def _fake_instance_id_for_pane(pane: str) -> str:
            """Stub the live @INSTANCE_ID stamp lookup tmuxctl would do."""
            return instance_id

        monkeypatch.setattr(app_env.main.shared, "instance_id_for_pane", _fake_instance_id_for_pane)

        resp = client.get("/api/panes/%25/instance")
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == instance_id
