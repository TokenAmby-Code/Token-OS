import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(app_env, monkeypatch):
    async def _no_pane_rows():
        return []

    monkeypatch.setattr(app_env.main, "_tmux_pane_rows", _no_pane_rows)
    return TestClient(app_env.main.app)


def _insert_ops_fixture(app_env):
    instance_id = str(uuid.uuid4())
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, tmux_pane, engine, registered_at, last_activity, zealotry)
           VALUES (?, ?, 'ops-test', '/tmp/ops', 'local', 'Mac-Mini',
                   'processing', '%44', 'codex',
                   '2026-05-25T10:00:00', '2026-05-25T10:01:00', 5)""",
        (instance_id, str(uuid.uuid4())),
    )
    conn.execute(
        "INSERT INTO events (event_type, instance_id, device_id, details, created_at) VALUES (?, ?, ?, ?, ?)",
        ("ops_test_event", instance_id, "Mac-Mini", '{"ok": true}', "2026-05-25T10:02:00"),
    )
    conn.commit()
    conn.close()
    return instance_id


def test_ops_state_returns_expected_top_level_keys(client, app_env):
    _insert_ops_fixture(app_env)

    resp = client.get("/api/ui/ops/state")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["surface"] == "ops"
    for key in (
        "generated_at",
        "timer",
        "assertions",
        "attention",
        "work_state",
        "instances",
        "events",
        "cron",
        "tts",
        "enforcement",
    ):
        assert key in body
    assert body["instances"]["counts"]["active"] == 1
    assert body["instances"]["active"][0]["display_name"] == "ops-test"
    assertion_ids = {item["id"] for item in body["assertions"]}
    assert {"timer_mode", "productivity", "desktop_attention", "phone_attention"}.issubset(
        assertion_ids
    )


def test_ops_timer_history_returns_live_shape(client, app_env):
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO timer_shifts
           (timestamp, old_mode, new_mode, trigger, source, break_balance_ms,
            break_backlog_ms, work_time_ms, active_instances, phone_app, details)
           VALUES (?, 'idle', 'working', 'test',
                   'pytest', 60000, 0, 0, 1, NULL, NULL)""",
        ((datetime.now() - timedelta(minutes=5)).isoformat(),),
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/ui/ops/timer/history?window=15m&bucket=60s")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "timer_shifts+live_timer_engine"
    assert body["window_seconds"] == 900
    assert body["bucket_seconds"] == 60
    assert len(body["points"]) >= 2
    assert body["points"][-1]["break_balance_ms"] == app_env.main.timer_engine.break_balance_ms
    assert body["segments"]
    assert body["annotations"][0]["type"] == "test"


def test_ops_ui_serves_index_html(client):
    resp = client.get("/ui/ops")

    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    assert "Ops Cockpit" in resp.text


def test_ops_ui_serves_built_asset(client, app_env):
    ops_dir = Path(app_env.main.UI_DIR) / "ops"
    asset = next(ops_dir.glob("assets/*"), None)
    assert asset is not None, "run npm run build in token-api/web/ops before backend tests"

    rel = asset.relative_to(ops_dir).as_posix()
    resp = client.get(f"/ui/ops/{rel}")

    assert resp.status_code == 200, resp.text
    assert resp.content


def test_ops_ui_unknown_asset_returns_404(client):
    resp = client.get("/ui/ops/assets/does-not-exist.js")

    assert resp.status_code == 404
