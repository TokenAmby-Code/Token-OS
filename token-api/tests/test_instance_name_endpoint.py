import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

# Pane -> instance_id mapping seeded by ``_insert_instance``. The rename route
# resolves a pane to its instance via the pane's live @INSTANCE_ID stamp
# (tmuxctl owns resolution); there is no stored tmux_pane column and no tmux
# server in tests, so the stubbed resolver consults this in-test map instead.
_PANE_STAMPS: dict[str, str] = {}


@pytest.fixture
def client(app_env, monkeypatch):
    _PANE_STAMPS.clear()

    async def _stamp(pane):
        return _PANE_STAMPS.get(pane)

    monkeypatch.setattr(app_env.main.shared, "instance_id_for_pane", _stamp)
    yield TestClient(app_env.main.app)
    _PANE_STAMPS.clear()


def _db(app_env):
    conn = sqlite3.connect(app_env.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_instance(app_env, *, tmux_pane="%77", status="idle", tab_name="Claude 13:14"):
    instance_id = str(uuid.uuid4())
    conn = _db(app_env)
    conn.execute(
        """INSERT INTO instances
           (id, name, working_dir, origin_type, device_id, status,
            engine, created_at, last_activity)
           VALUES (?, ?, '/tmp', 'local', 'Mac-Mini', ?, 'codex',
                   '2026-05-10T13:00:00', '2026-05-10T13:00:00')""",
        (instance_id, tab_name, status),
    )
    conn.commit()
    conn.close()
    # The live pane stamp resolves to this instance only while it is active —
    # mirror the route's status guard by stamping every row, since the route
    # itself rejects stopped/archived rows after resolution.
    _PANE_STAMPS[tmux_pane] = instance_id
    return instance_id


def test_instance_name_endpoint_renames_active_pane(client, app_env):
    instance_id = _insert_instance(app_env)

    resp = client.post(
        "/api/instance/rename",
        json={"pane_id": "%77", "name": "anti-archaeology-cli"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["instance_id"] == instance_id
    assert body["name"] == "anti-archaeology-cli"

    conn = _db(app_env)
    row = conn.execute(
        "SELECT name AS tab_name FROM instances WHERE id = ?", (instance_id,)
    ).fetchone()
    mutation = conn.execute(
        "SELECT mutation_type, actor, field_names_json FROM instance_mutations WHERE instance_id = ? ORDER BY id DESC LIMIT 1",
        (instance_id,),
    ).fetchone()
    conn.close()

    assert row["tab_name"] == "anti-archaeology-cli"
    assert mutation["mutation_type"] == "instance_updated"
    assert mutation["actor"] == "instance-name-cli"
    assert "name" in mutation["field_names_json"]


def test_rename_enqueues_exactly_one_pane_label_row(client, app_env):
    """The rename route commits ``instances.name``, firing ``trg_tab_name_pane_state``
    into exactly one ``@PANE_LABEL`` queue row. The trigger is the single writer of the
    border-nametag intent; the semantic-rename cutover left it untouched."""
    instance_id = _insert_instance(app_env)

    resp = client.post(
        "/api/instance/rename",
        json={"pane_id": "%77", "name": "border-nametag-name"},
    )
    assert resp.status_code == 200, resp.text

    conn = _db(app_env)
    rows = conn.execute(
        "SELECT instance_id, variable, value FROM pane_state_queue WHERE variable = '@PANE_LABEL'",
    ).fetchall()
    conn.close()

    assert [tuple(r) for r in rows] == [(instance_id, "@PANE_LABEL", "border-nametag-name")]


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
        json={"pane_id": "%77", "name": tab_name},
    )

    assert resp.status_code == 400
    assert detail in resp.json()["detail"]


def test_instance_name_endpoint_requires_matching_active_pane(client, app_env):
    _insert_instance(app_env, tmux_pane="%stopped", status="stopped")

    resp = client.post(
        "/api/instance/rename",
        json={"pane_id": "%stopped", "name": "valid-name"},
    )

    assert resp.status_code == 404


def test_instance_name_endpoint_requires_pane_id(client):
    resp = client.post(
        "/api/instance/rename",
        json={"pane_id": "", "name": "valid-name"},
    )

    assert resp.status_code == 400
    assert "pane_id" in resp.json()["detail"]
