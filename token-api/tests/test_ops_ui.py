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
        """INSERT INTO instances
           (id, name, working_dir, origin_type, device_id,
            status, engine, created_at, last_activity, zealotry)
           VALUES (?, 'ops-test', '/tmp/ops', 'local', 'Mac-Mini',
                   'working', 'codex',
                   '2026-05-25T10:00:00', '2026-05-25T10:01:00', 5)""",
        (instance_id,),
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
        "source_freshness",
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
    assert "by_persona" in body["instances"]["counts"]
    assert isinstance(body["instances"]["counts"]["by_persona"], dict)
    assert "by_legion" not in body["instances"]["counts"]
    assert body["instances"]["active"][0]["display_name"] == "ops-test"
    assertion_ids = {item["id"] for item in body["assertions"]}
    assert {"timer_mode", "productivity", "desktop_attention", "phone_attention"}.issubset(
        assertion_ids
    )
    assert set(body["source_freshness"]) == {
        "desktop_attention",
        "phone_activity",
        "phone_heartbeat",
        "work_state",
        "timer_engine",
        "agents_db",
        "tmuxctld",
        "cron",
        "enforcement",
        "tts",
    }
    freshness = body["source_freshness"]["phone_heartbeat"]
    assert set(freshness) == {
        "status",
        "age_seconds",
        "last_seen",
        "stale_after_seconds",
        "message",
        "evidence",
    }


def test_ops_status_returns_expected_top_level_keys(client, app_env):
    _insert_ops_fixture(app_env)

    resp = client.get("/api/ops/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["surface"] == "ops-status"
    for key in (
        "generated_at",
        "status",
        "summary",
        "sources",
        "source_freshness",
        "timer",
        "attention",
        "fleet",
        "tmux",
        "tts",
        "enforcement",
        "assertions",
        "recommended_actions",
    ):
        assert key in body
    assert set(body["sources"]) == {
        "token_api",
        "agents_db",
        "timer_engine",
        "tmuxctld",
        "cron",
        "enforcement",
        "tts",
    }
    assert body["fleet"]["active"] == 1
    assert body["fleet"]["by_engine"]["codex"] == 1
    assert body["fleet"]["by_persona"]["astartes"] == 1
    assert set(body["source_freshness"]) == {
        "desktop_attention",
        "phone_activity",
        "phone_heartbeat",
        "work_state",
        "timer_engine",
        "agents_db",
        "tmuxctld",
        "cron",
        "enforcement",
        "tts",
    }


def test_ops_source_freshness_marks_stale_phone_heartbeat(client, app_env):
    app_env.main.PHONE_HEARTBEAT["last_seen"] = datetime.now() - timedelta(minutes=20)
    app_env.main.PHONE_HEARTBEAT["device_id"] = "pytest-phone"

    resp = client.get("/api/ui/ops/state")

    assert resp.status_code == 200, resp.text
    heartbeat = resp.json()["source_freshness"]["phone_heartbeat"]
    assert heartbeat["status"] == "stale"
    assert heartbeat["age_seconds"] >= 20 * 60
    assert heartbeat["stale_after_seconds"] == 600


def test_ops_source_freshness_missing_desktop_timestamp_does_not_crash(client, app_env):
    app_env.main.DESKTOP_STATE["last_detection"] = None

    resp = client.get("/api/ops/status")

    assert resp.status_code == 200, resp.text
    desktop = resp.json()["source_freshness"]["desktop_attention"]
    assert desktop["status"] in {"missing", "unknown"}
    assert desktop["age_seconds"] is None


def test_ops_instances_include_attention_rank_and_sort_by_urgency(client, app_env):
    now = datetime.now()
    conn = sqlite3.connect(app_env.db_path)
    stale_idle_id = str(uuid.uuid4())
    working_id = str(uuid.uuid4())
    conn.executemany(
        """INSERT INTO instances
           (id, name, working_dir, origin_type, device_id,
            status, engine, created_at, last_activity)
           VALUES (?, ?, '/tmp/ops', 'local', 'Mac-Mini',
                   ?, 'codex', ?, ?)""",
        [
            (
                working_id,
                "fresh-working",
                "working",
                (now - timedelta(minutes=2)).isoformat(),
                (now - timedelta(minutes=1)).isoformat(),
            ),
            (
                stale_idle_id,
                "stale-idle",
                "idle",
                (now - timedelta(hours=4)).isoformat(),
                (now - timedelta(hours=3)).isoformat(),
            ),
        ],
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/ui/ops/state")

    assert resp.status_code == 200, resp.text
    active = resp.json()["instances"]["active"]
    assert active[0]["id"] == stale_idle_id
    assert active[0]["attention_rank"] == 1
    assert active[0]["attention_reasons"]
    assert active[0]["stale"]["is_stale"] is True
    fresh = next(item for item in active if item["id"] == working_id)
    assert fresh["attention_rank"] == 5


def test_ops_status_negative_break_balance_is_bad(client, app_env):
    app_env.main.timer_engine._break_balance_ms = -60_000

    resp = client.get("/api/ops/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["timer"]["is_in_backlog"] is True
    assert body["timer"]["break_backlog_ms"] == 60_000
    assert body["status"] == "bad"
    break_assertion = next(item for item in body["assertions"] if item["id"] == "break_balance")
    assert break_assertion["status"] == "bad"


def test_ops_status_pending_enforcement_recommends_action(client, app_env):
    conn = sqlite3.connect(app_env.db_path)
    now = datetime.now()
    conn.execute(
        """INSERT INTO expected_acknowledgements
           (id, source, instance_id, reason, status, created_at,
            ack_due_at, level2_due_at, pavlok_due_at, fired_levels_json, details_json)
           VALUES (?, 'pytest', NULL, 'confirm test', 'pending', ?, ?, ?, ?, '[]', '{}')""",
        (
            str(uuid.uuid4()),
            now.isoformat(),
            (now - timedelta(seconds=1)).isoformat(),
            (now + timedelta(minutes=1)).isoformat(),
            (now + timedelta(minutes=2)).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/ops/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "bad"
    assert body["enforcement"]["pending_count"] == 1
    enforcement_assertion = next(item for item in body["assertions"] if item["id"] == "enforcement")
    assert enforcement_assertion["status"] == "bad"
    assert any(
        action["source_assertion_id"] == "enforcement" and "acknowledge/resolve" in action["action"]
        for action in body["recommended_actions"]
    )


def test_ops_status_tmuxctld_health_unavailable_degrades_without_crash(client):
    resp = client.get("/api/ops/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sources"]["tmuxctld"]["status"] == "warn"
    assert body["sources"]["tmuxctld"]["available"] is False
    assert body["tmux"]["reachable"] is False
    assert body["tmux"]["tmux_reachable"] is None


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
        """INSERT INTO instances
           (id, name, working_dir, origin_type, device_id,
            status, engine, created_at, last_activity)
           VALUES (?, 'stale-idle', '/tmp/ops', 'local', 'Mac-Mini',
                   'idle', 'codex',
                   datetime('now', '-20 minutes'), datetime('now', '-10 minutes'))""",
        (str(uuid.uuid4()),),
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
