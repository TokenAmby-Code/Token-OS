import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(app_env):
    return TestClient(app_env.main.app)


def _db(app_env):
    c = sqlite3.connect(app_env.db_path)
    c.row_factory = sqlite3.Row
    return c


def _insert_instance(app_env, *, status="idle", tab_name="Claude 13:14"):
    iid = str(uuid.uuid4())
    c = _db(app_env)
    c.execute(
        """INSERT INTO claude_instances (id, session_id, tab_name, working_dir, origin_type, device_id, status, tmux_pane, engine, registered_at, last_activity) VALUES (?, ?, ?, '/tmp', 'local', 'Mac-Mini', ?, '%77', 'codex', '2026-05-10T13:00:00', '2026-05-10T13:00:00')""",
        (iid, str(uuid.uuid4()), tab_name, status),
    )
    c.commit()
    c.close()
    return iid


def test_instance_rename_endpoint_renames_by_instance_id(client, app_env):
    iid = _insert_instance(app_env)
    r = client.patch(f"/api/instances/{iid}/rename", json={"tab_name": "anti archaeology cli"})
    assert r.status_code == 200, r.text
    assert r.json()["tab_name"] == "anti-archaeology-cli"
    c = _db(app_env)
    row = c.execute("SELECT tab_name FROM claude_instances WHERE id=?", (iid,)).fetchone()
    mut = c.execute(
        "SELECT mutation_type, actor, field_names_json FROM instance_mutations WHERE instance_id=? ORDER BY id DESC LIMIT 1",
        (iid,),
    ).fetchone()
    c.close()
    assert row["tab_name"] == "anti-archaeology-cli"
    assert mut["actor"] == "rename-instance"
    assert "tab_name" in mut["field_names_json"]


@pytest.mark.parametrize("tab_name", ["", "   ", "???"])
def test_instance_rename_endpoint_rejects_empty_after_normalization(client, app_env, tab_name):
    iid = _insert_instance(app_env)
    r = client.patch(f"/api/instances/{iid}/rename", json={"tab_name": tab_name})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]
