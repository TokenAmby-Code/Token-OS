import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(app_env):
    return TestClient(app_env.main.app)


def _db(app_env):
    conn = sqlite3.connect(app_env.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_instance(app_env, *, status="idle", tab_name="Claude 13:14"):
    instance_id = str(uuid.uuid4())
    conn = _db(app_env)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, tmux_pane, engine, registered_at, last_activity)
           VALUES (?, ?, ?, '/tmp', 'local', 'Mac-Mini', ?, '%77', 'codex',
                   '2026-05-10T13:00:00', '2026-05-10T13:00:00')""",
        (instance_id, str(uuid.uuid4()), tab_name, status),
    )
    conn.commit()
    conn.close()
    return instance_id


def test_instance_rename_endpoint_renames_by_instance_id(client, app_env):
    instance_id = _insert_instance(app_env)

    resp = client.patch(
        f"/api/instances/{instance_id}/rename",
        json={"tab_name": "anti archaeology cli"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["tab_name"] == "anti-archaeology-cli"

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
    assert mutation["actor"] == "rename-instance"
    assert "tab_name" in mutation["field_names_json"]


@pytest.mark.parametrize("tab_name", ["", "   ", "???"])
def test_instance_rename_endpoint_rejects_empty_after_normalization(client, app_env, tab_name):
    instance_id = _insert_instance(app_env)

    resp = client.patch(
        f"/api/instances/{instance_id}/rename",
        json={"tab_name": tab_name},
    )

    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"]
