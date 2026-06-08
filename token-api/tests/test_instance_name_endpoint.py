import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(app_env, monkeypatch):
    async def _noop_refresh(tmux_pane):
        return None

    monkeypatch.setattr(app_env.main, "_refresh_tmux_pane_label", _noop_refresh)
    return TestClient(app_env.main.app)


def _db(app_env):
    conn = sqlite3.connect(app_env.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_instance(app_env, *, tmux_pane="%77", status="idle", tab_name="Claude 13:14"):
    instance_id = str(uuid.uuid4())
    conn = _db(app_env)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, tmux_pane, engine, registered_at, last_activity)
           VALUES (?, ?, ?, '/tmp', 'local', 'Mac-Mini', ?, ?, 'codex',
                   '2026-05-10T13:00:00', '2026-05-10T13:00:00')""",
        (instance_id, str(uuid.uuid4()), tab_name, status, tmux_pane),
    )
    conn.commit()
    conn.close()
    return instance_id


def test_instance_name_endpoint_renames_active_pane(client, app_env):
    instance_id = _insert_instance(app_env)

    resp = client.post(
        "/api/instance/rename",
        json={"tmux_pane": "%77", "tab_name": "anti-archaeology-cli"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["instance_id"] == instance_id
    assert body["tab_name"] == "anti-archaeology-cli"

    conn = _db(app_env)
    row = conn.execute(
        "SELECT tab_name FROM claude_instances WHERE id = ?", (instance_id,)
    ).fetchone()
    mutation = conn.execute(
        "SELECT mutation_type, actor, field_names_json FROM instance_mutations WHERE instance_id = ? ORDER BY id DESC LIMIT 1",
        (instance_id,),
    ).fetchone()
    conn.close()

    assert row["tab_name"] == "anti-archaeology-cli"
    assert mutation["mutation_type"] == "instance_updated"
    assert mutation["actor"] == "instance-name-cli"
    assert "tab_name" in mutation["field_names_json"]


@pytest.mark.parametrize(
    "tab_name,detail",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("Claude 13:14", "placeholder"),
        ("✳ Claude 13:14", "placeholder"),
        ("x" * 41, "40 characters"),
    ],
)
def test_instance_name_endpoint_rejects_invalid_names(client, app_env, tab_name, detail):
    _insert_instance(app_env)

    resp = client.post(
        "/api/instance/rename",
        json={"tmux_pane": "%77", "tab_name": tab_name},
    )

    assert resp.status_code == 400
    assert detail in resp.json()["detail"]


def test_instance_name_endpoint_requires_matching_active_pane(client, app_env):
    _insert_instance(app_env, tmux_pane="%stopped", status="stopped")

    resp = client.post(
        "/api/instance/rename",
        json={"tmux_pane": "%stopped", "tab_name": "valid-name"},
    )

    assert resp.status_code == 404


def test_instance_name_endpoint_requires_tmux_pane(client):
    resp = client.post(
        "/api/instance/rename",
        json={"tmux_pane": "", "tab_name": "valid-name"},
    )

    assert resp.status_code == 400
    assert "tmux_pane" in resp.json()["detail"]
