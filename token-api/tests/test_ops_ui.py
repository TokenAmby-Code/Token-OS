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
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, engine, registered_at, last_activity, zealotry)
           VALUES (?, ?, 'ops-test', '/tmp/ops', 'local', 'Mac-Mini',
                   'processing', 'codex',
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
    now = datetime.now()
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO timer_shifts
           (timestamp, old_mode, new_mode, trigger, source, break_balance_ms,
            break_backlog_ms, work_time_ms, active_instances, phone_app, details)
           VALUES (?, 'idle', 'working', 'test',
                   'pytest', 60000, 0, 0, 1, NULL, NULL)""",
        ((now - timedelta(minutes=5)).isoformat(),),
    )
    conn.executemany(
        """INSERT INTO timer_samples
           (timestamp, mode, activity, productivity_active, break_balance_ms,
            break_backlog_ms, work_time_ms, active_instance_count,
            processing_recent_count, observed_agent_count, desktop_mode,
            phone_app, source)
           VALUES (?, 'working', 'working', 1, ?, 0, 0, 1, 1, 1,
                   'silence', NULL, 'pytest')""",
        [
            ((now - timedelta(seconds=60)).isoformat(), 60000),
            ((now - timedelta(seconds=30)).isoformat(), 90000),
        ],
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/ui/ops/timer/history?window=15m&bucket=60s")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "timer_samples+timer_shifts+live_timer_engine"
    assert body["window_seconds"] == 900
    assert body["bucket_seconds"] == 60
    assert len(body["points"]) >= 2
    assert body["points"][0]["sample_source"] == "pytest"
    assert body["points"][-1]["break_balance_ms"] == app_env.main.timer_engine.break_balance_ms
    assert body["segments"]
    assert body["annotations"][0]["type"] == "test"


def test_ops_timer_history_does_not_interpolate_sparse_shifts(client, app_env):
    now = datetime.now()
    app_env.main.timer_engine._break_balance_ms = -4560000
    conn = sqlite3.connect(app_env.db_path)
    conn.executemany(
        """INSERT INTO timer_shifts
           (timestamp, old_mode, new_mode, trigger, source, break_balance_ms,
            break_backlog_ms, work_time_ms, active_instances, phone_app, details)
           VALUES (?, ?, ?, 'restart_regression', 'pytest', ?, 0, 0, 1, NULL, NULL)""",
        [
            ((now - timedelta(minutes=82)).isoformat(), "working", "break", 300000),
            ((now - timedelta(minutes=1)).isoformat(), "break", "working", -4560000),
        ],
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/ui/ops/timer/history?window=2h&bucket=60s")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["annotations"]) == 2
    assert len(body["points"]) == 3
    assert body["points"][0]["sample_source"] == "timer_shift"
    assert body["points"][0]["gap_before"] is True
    assert body["points"][-1]["sample_source"] == "live_timer_engine"
    assert body["points"][-1]["gap_before"] is True
    assert body["gaps"]
    assert body["gaps"][0]["reason"] == "no_timer_samples"
    assert body["segments"] == []


def test_ops_timer_history_marks_restart_sample_gap(client, app_env):
    now = datetime.now()
    conn = sqlite3.connect(app_env.db_path)
    conn.executemany(
        """INSERT INTO timer_samples
           (timestamp, mode, activity, productivity_active, break_balance_ms,
            break_backlog_ms, work_time_ms, active_instance_count,
            processing_recent_count, observed_agent_count, desktop_mode,
            phone_app, source)
           VALUES (?, 'working', 'working', 1, ?, 0, 0, 1, 1, 1,
                   'silence', NULL, 'pytest')""",
        [
            ((now - timedelta(minutes=4)).isoformat(), 60000),
            ((now - timedelta(seconds=30)).isoformat(), 90000),
        ],
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/ui/ops/timer/history?window=15m&bucket=60s")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert any(gap["reason"] == "sample_gap" for gap in body["gaps"])
    assert body["points"][1].get("gap_before") is True


def test_ops_timer_history_flags_impossible_rate(client, app_env):
    now = datetime.now()
    app_env.main.timer_engine._break_balance_ms = 240000
    conn = sqlite3.connect(app_env.db_path)
    conn.executemany(
        """INSERT INTO timer_samples
           (timestamp, mode, activity, productivity_active, break_balance_ms,
            break_backlog_ms, work_time_ms, active_instance_count,
            processing_recent_count, observed_agent_count, desktop_mode,
            phone_app, source)
           VALUES (?, 'working', 'working', 1, ?, 0, 0, 1, 1, 1,
                   'silence', NULL, 'pytest')""",
        [
            ((now - timedelta(seconds=50)).isoformat(), 0),
            ((now - timedelta(seconds=20)).isoformat(), 240000),
        ],
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/ui/ops/timer/history?window=15m&bucket=60s")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["anomaly_summary"]["count"] >= 1
    impossible = [p for p in body["points"] if p.get("anomaly_reason") == "impossible_rate"]
    assert impossible
    assert impossible[0]["gap_before"] is True


def test_ops_timer_history_marks_sparse_large_delta_as_gap(client, app_env):
    now = datetime.now()
    app_env.main.timer_engine._break_balance_ms = -120000
    conn = sqlite3.connect(app_env.db_path)
    conn.executemany(
        """INSERT INTO timer_samples
           (timestamp, mode, activity, productivity_active, break_balance_ms,
            break_backlog_ms, work_time_ms, active_instance_count,
            processing_recent_count, observed_agent_count, desktop_mode,
            phone_app, source)
           VALUES (?, 'break', 'distraction', 0, ?, 0, 0, 0, 0, 0,
                   'video', NULL, 'pytest')""",
        [
            ((now - timedelta(minutes=8)).isoformat(), 300000),
            ((now - timedelta(minutes=1)).isoformat(), -120000),
        ],
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/ui/ops/timer/history?window=15m&bucket=60s")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert any(gap.get("anomaly_reason") == "sparse_large_delta" for gap in body["gaps"])
    sparse_points = [p for p in body["points"] if p.get("anomaly_reason") == "sparse_large_delta"]
    assert sparse_points
    assert sparse_points[0]["gap_before"] is True


def test_ops_timer_history_allows_reset_discontinuity(client, app_env):
    now = datetime.now()
    app_env.main.timer_engine._break_balance_ms = 0
    reset_at = now - timedelta(seconds=25)
    conn = sqlite3.connect(app_env.db_path)
    conn.executemany(
        """INSERT INTO timer_samples
           (timestamp, mode, activity, productivity_active, break_balance_ms,
            break_backlog_ms, work_time_ms, active_instance_count,
            processing_recent_count, observed_agent_count, desktop_mode,
            phone_app, source)
           VALUES (?, 'working', 'working', 1, ?, 0, 0, 1, 1, 1,
                   'silence', NULL, 'pytest')""",
        [
            ((now - timedelta(seconds=40)).isoformat(), 300000),
            ((now - timedelta(seconds=10)).isoformat(), 0),
        ],
    )
    conn.execute(
        """INSERT INTO timer_shifts
           (timestamp, old_mode, new_mode, trigger, source, break_balance_ms,
            break_backlog_ms, work_time_ms, active_instances, phone_app, details)
           VALUES (?, 'working', 'working', 'daily_reset',
                   'pytest', 0, 0, 0, 1, NULL, NULL)""",
        (reset_at.isoformat(),),
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/ui/ops/timer/history?window=15m&bucket=60s")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert any(gap["reason"] == "reset_discontinuity" for gap in body["gaps"])
    assert not any(a["reason"] == "impossible_rate" for a in body["anomalies"])


def test_ops_timer_history_handles_missing_timer_samples_table(client, app_env):
    now = datetime.now()
    app_env.main.timer_engine._break_balance_ms = 120000
    conn = sqlite3.connect(app_env.db_path)
    conn.execute("DROP TABLE timer_samples")
    conn.execute(
        """INSERT INTO timer_shifts
           (timestamp, old_mode, new_mode, trigger, source, break_balance_ms,
            break_backlog_ms, work_time_ms, active_instances, phone_app, details)
           VALUES (?, 'idle', 'break', 'pytest',
                   'pytest', 120000, 0, 0, 1, NULL, NULL)""",
        ((now - timedelta(minutes=2)).isoformat(),),
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/ui/ops/timer/history?window=15m&bucket=60s")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert any(gap["reason"] == "no_timer_samples_table" for gap in body["gaps"])
    assert body["points"][0]["sample_source"] == "timer_shift"
    assert body["segments"] == []


def test_ops_timer_history_may_28_sparse_snap_regression(client, app_env):
    """07:07 near-zero → 09:56 deep backlog is shown as a gap, not a snap line."""
    now = datetime.now()
    app_env.main.timer_engine._break_balance_ms = -10_140_000
    first = now - timedelta(hours=3)
    second = first + timedelta(hours=2, minutes=49)
    conn = sqlite3.connect(app_env.db_path)
    conn.executemany(
        """INSERT INTO timer_shifts
           (timestamp, old_mode, new_mode, trigger, source, break_balance_ms,
            break_backlog_ms, work_time_ms, active_instances, phone_app, details)
           VALUES (?, ?, ?, ?, 'pytest', ?, 0, 0, 1, NULL, NULL)""",
        [
            (first.isoformat(), "break", "break", "break_exhausted", 0),
            (second.isoformat(), "break", "working", "productivity_active", -10_140_000),
        ],
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/ui/ops/timer/history?window=4h&bucket=60s")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["segments"] == []
    assert any(gap.get("anomaly_reason") == "sparse_large_delta" for gap in body["gaps"])


def test_work_state_ignores_stale_idle_instances(client, app_env):
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, engine, registered_at, last_activity)
           VALUES (?, ?, 'stale-idle', '/tmp/ops', 'local', 'Mac-Mini',
                   'idle', 'codex',
                   datetime('now', '-20 minutes'), datetime('now', '-10 minutes'))""",
        (str(uuid.uuid4()), str(uuid.uuid4())),
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/work-state")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["productivity_active"] is False
    assert body["reason"] == "no_recent_work_activity"


def test_work_action_sets_short_productivity_window(client):
    resp = client.post("/api/work-action", json={"source": "pytest", "note": "state assertion"})
    assert resp.status_code == 200, resp.text

    state_resp = client.get("/api/work-state")

    assert state_resp.status_code == 200, state_resp.text
    body = state_resp.json()
    assert body["productivity_active"] is True
    assert body["reason"] == "recent_work_action"


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
