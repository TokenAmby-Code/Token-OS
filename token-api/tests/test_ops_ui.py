import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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


def test_ops_state_returns_expected_top_level_keys(client, app_env) -> None:
    _insert_ops_fixture(app_env)

    resp = client.get("/api/ui/ops/state")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["surface"] == "ops"
    for key in (
        "contract_version",
        "generated_at",
        "health",
        "sources",
        "timer",
        "assertions",
        "recommended_actions",
        "source_freshness",
        "attention",
        "work_state",
        "instances",
        "events",
        "cron",
        "tts",
        "enforcement",
        "tmux",
    ):
        assert key in body
    assert body["contract_version"] == "ops-state.v1"
    assert body["health"]["status"] in {"ok", "warn", "bad", "unknown"}
    assert isinstance(body["health"]["summary"], str)
    assert isinstance(body["health"]["degraded_sources"], list)
    assert body["recommended_actions"] == body["health"]["recommended_actions"]
    assert set(body["sources"]) == {
        "token_api",
        "agents_db",
        "timer_engine",
        "tmuxctld",
        "cron",
        "enforcement",
        "tts",
    }
    assert "reachable" in body["tmux"]
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


def test_ops_state_health_includes_typed_tmuxctld_status(client, app_env, monkeypatch) -> None:
    async def _fake_tmuxctld_health():
        return {
            "reachable": True,
            "tmux_reachable": True,
            "version": "pytest-tmuxctld",
            "sha": "abc1234",
            "error": None,
        }

    monkeypatch.setattr(app_env.main, "_ops_read_tmuxctld_health", _fake_tmuxctld_health)

    resp = client.get("/api/ui/ops/state")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tmux"]["reachable"] is True
    assert body["tmux"]["tmux_reachable"] is True
    assert body["tmux"]["version"] == "pytest-tmuxctld"
    assert body["sources"]["tmuxctld"]["status"] == "ok"
    assert body["source_freshness"]["tmuxctld"]["status"] == "fresh"


def test_ops_state_health_recommends_actions_for_bad_assertions(client, app_env) -> None:
    app_env.main.timer_engine._break_balance_ms = -60_000
    now = datetime.now()
    conn = sqlite3.connect(app_env.db_path)
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

    resp = client.get("/api/ui/ops/state")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["health"]["status"] == "bad"
    assert body["health"]["bad_assertion_count"] >= 2
    action_ids = {action["source_assertion_id"] for action in body["health"]["recommended_actions"]}
    assert {"break_balance", "enforcement"}.issubset(action_ids)


def test_ops_status_returns_expected_top_level_keys(client, app_env) -> None:
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


def test_ops_active_fleet_graph_returns_live_relationships(client, app_env, monkeypatch) -> None:
    instance_id = str(uuid.uuid4())
    conn = sqlite3.connect(app_env.db_path)
    doc_id = conn.execute(
        """INSERT INTO session_documents
           (title, file_path, project, status, created_at, updated_at)
           VALUES ('Ops Graph Doc', 'Sessions/ops-graph-doc.md', 'ops', 'active',
                   '2026-05-25T10:00:00', '2026-05-25T10:00:00')"""
    ).lastrowid
    conn.execute(
        """INSERT INTO instances
           (id, name, working_dir, origin_type, device_id,
            status, engine, created_at, last_activity, session_doc_id)
           VALUES (?, 'ops-graph-instance', '/tmp/ops', 'local', 'Mac-Mini',
                   'working', 'codex',
                   '2026-05-25T10:00:00', '2026-05-25T10:01:00', ?)""",
        (instance_id, doc_id),
    )
    conn.commit()
    conn.close()

    async def _live_agent_panes():
        return [
            {
                "instance_id": instance_id,
                "pane_id": "%42",
                "pane_label": "ops-pane",
                "pane_role": "codex",
                "current_command": "codex",
            }
        ]

    monkeypatch.setattr(app_env.main, "_live_agent_panes", _live_agent_panes)

    resp = client.get("/api/ui/ops/graph/active-fleet")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["graph"] == "active-fleet"
    assert body["generated_at"]
    assert body["layout_hint"] == "dagre"

    node_ids = {node["id"] for node in body["nodes"]}
    assert f"instance:{instance_id}" in node_ids
    assert "device:Mac-Mini" in node_ids
    assert f"session_doc:{doc_id}" in node_ids
    assert "pane:%42" in node_ids

    edges = {(edge["source"], edge["target"], edge["type"]) for edge in body["edges"]}
    assert ("device:Mac-Mini", f"instance:{instance_id}", "hosts") in edges
    assert (f"instance:{instance_id}", f"session_doc:{doc_id}", "bound_to") in edges
    assert (f"instance:{instance_id}", "pane:%42", "runs_on") in edges

    alias = client.get("/api/ui/ops/graph/active")
    assert alias.status_code == 200, alias.text
    alias_body = alias.json()
    assert alias_body["graph"] == "active-fleet"
    assert {node["id"] for node in alias_body["nodes"]} == node_ids
    assert {edge["id"] for edge in alias_body["edges"]} == {edge["id"] for edge in body["edges"]}


def _insert_gt_graph_instance(app_env, *, session_doc: bool = False) -> tuple[str, str, int | None]:
    instance_id = str(uuid.uuid4())
    conn = sqlite3.connect(app_env.db_path)
    marker = str(conn.execute("INSERT INTO golden_throne DEFAULT VALUES").lastrowid)
    doc_id = None
    if session_doc:
        doc_id = conn.execute(
            """INSERT INTO session_documents
               (title, file_path, project, status, created_at, updated_at)
               VALUES ('GT Graph Doc', 'Sessions/gt-graph-doc.md', 'ops', 'active',
                       '2026-05-25T10:00:00', '2026-05-25T10:00:00')"""
        ).lastrowid
    conn.execute(
        """INSERT INTO instances
           (id, name, working_dir, origin_type, device_id,
            status, engine, created_at, last_activity, golden_throne, session_doc_id)
           VALUES (?, 'gt-graph-instance', '/tmp/ops', 'local', 'Mac-Mini',
                   'idle', 'codex',
                   '2026-05-25T10:00:00', '2026-05-25T10:01:00', ?, ?)""",
        (instance_id, marker, doc_id),
    )
    conn.commit()
    conn.close()
    return instance_id, marker, doc_id


def test_ops_golden_throne_graph_returns_valid_ops_graph(client, app_env) -> None:
    resp = client.get("/api/ui/ops/graph/golden-throne")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["graph"] == "golden-throne"
    assert body["generated_at"]
    assert body["layout_hint"] == "dagre"
    assert isinstance(body["nodes"], list)
    assert isinstance(body["edges"], list)


def test_ops_golden_throne_graph_gt_alias(client, app_env) -> None:
    _insert_gt_graph_instance(app_env)

    resp = client.get("/api/ui/ops/graph/gt")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["graph"] == "golden-throne"


def test_ops_golden_throne_graph_bound_instances_have_schedule_edges(client, app_env) -> None:
    instance_id, marker, _ = _insert_gt_graph_instance(app_env)

    resp = client.get("/api/ui/ops/graph/golden-throne")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    node_ids = {node["id"] for node in body["nodes"]}
    assert f"instance:{instance_id}" in node_ids
    assert f"golden_throne:{marker}" in node_ids
    edges = {(edge["source"], edge["target"], edge["type"]) for edge in body["edges"]}
    assert (f"golden_throne:{marker}", f"instance:{instance_id}", "scheduled") in edges


def test_ops_golden_throne_graph_session_doc_binding(client, app_env) -> None:
    instance_id, _, doc_id = _insert_gt_graph_instance(app_env, session_doc=True)

    resp = client.get("/api/ui/ops/graph/golden-throne")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    node_ids = {node["id"] for node in body["nodes"]}
    assert f"session_doc:{doc_id}" in node_ids
    edges = {(edge["source"], edge["target"], edge["type"]) for edge in body["edges"]}
    assert (f"instance:{instance_id}", f"session_doc:{doc_id}", "bound_to") in edges


def test_ops_golden_throne_graph_pending_acknowledgements(client, app_env) -> None:
    instance_id, _, _ = _insert_gt_graph_instance(app_env)
    ack_id = str(uuid.uuid4())
    now = datetime.now()
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO expected_acknowledgements
           (id, source, instance_id, reason, status, created_at,
            ack_due_at, level2_due_at, pavlok_due_at, fired_levels_json, details_json)
           VALUES (?, 'golden_throne', ?, 'confirm GT resume', 'pending', ?, ?, ?, ?, '[]', '{}')""",
        (
            ack_id,
            instance_id,
            now.isoformat(),
            (now - timedelta(seconds=1)).isoformat(),
            (now + timedelta(minutes=1)).isoformat(),
            (now + timedelta(minutes=2)).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/ui/ops/graph/golden-throne")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    node_ids = {node["id"] for node in body["nodes"]}
    assert f"ack:{ack_id}" in node_ids
    edges = {(edge["source"], edge["target"], edge["type"]) for edge in body["edges"]}
    assert (f"instance:{instance_id}", f"ack:{ack_id}", "ack_required") in edges


def test_ops_source_freshness_marks_stale_phone_heartbeat(client, app_env, monkeypatch) -> None:
    monkeypatch.setitem(
        app_env.main.PHONE_HEARTBEAT,
        "last_seen",
        datetime.now() - timedelta(minutes=20),
    )
    monkeypatch.setitem(app_env.main.PHONE_HEARTBEAT, "device_id", "pytest-phone")

    resp = client.get("/api/ui/ops/state")

    assert resp.status_code == 200, resp.text
    heartbeat = resp.json()["source_freshness"]["phone_heartbeat"]
    assert heartbeat["status"] == "stale"
    assert heartbeat["age_seconds"] >= 20 * 60
    assert heartbeat["stale_after_seconds"] == 600


def test_ops_source_freshness_missing_desktop_timestamp_does_not_crash(
    client, app_env, monkeypatch
) -> None:
    monkeypatch.setitem(app_env.main.DESKTOP_STATE, "last_detection", None)

    resp = client.get("/api/ops/status")

    assert resp.status_code == 200, resp.text
    desktop = resp.json()["source_freshness"]["desktop_attention"]
    assert desktop["status"] in {"missing", "unknown"}
    assert desktop["age_seconds"] is None


def test_ops_source_freshness_marks_timer_and_work_state_unknown_on_timer_error(
    client, app_env, monkeypatch
) -> None:
    async def _fail_work_state() -> None:
        raise RuntimeError("pytest timer/work-state failure")

    monkeypatch.setattr(app_env.main, "get_cached_work_state", _fail_work_state)

    resp = client.get("/api/ui/ops/state")

    assert resp.status_code == 200, resp.text
    freshness = resp.json()["source_freshness"]
    for source in ("work_state", "timer_engine"):
        record = freshness[source]
        assert record["status"] == "unknown"
        assert record["last_seen"] is None
        assert record["age_seconds"] is None
        assert any("pytest timer/work-state failure" in item for item in record["evidence"])


def test_ops_instances_include_attention_rank_and_sort_by_urgency(client, app_env) -> None:
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
    assert fresh["attention_rank"] == 6


def test_ops_instances_split_golden_throne_due_armed_and_processing_ranks(
    client, app_env, monkeypatch
) -> None:
    now = datetime.now()
    due_id = str(uuid.uuid4())
    armed_id = str(uuid.uuid4())
    processing_id = str(uuid.uuid4())
    idle_id = str(uuid.uuid4())
    conn = sqlite3.connect(app_env.db_path)
    due_marker = str(conn.execute("INSERT INTO golden_throne DEFAULT VALUES").lastrowid)
    armed_marker = str(conn.execute("INSERT INTO golden_throne DEFAULT VALUES").lastrowid)
    conn.executemany(
        """INSERT INTO instances
           (id, name, working_dir, origin_type, device_id,
            status, engine, created_at, last_activity, golden_throne)
           VALUES (?, ?, '/tmp/ops', 'local', 'Mac-Mini',
                   ?, 'codex', ?, ?, ?)""",
        [
            (due_id, "gt-due", "idle", now.isoformat(), now.isoformat(), due_marker),
            (armed_id, "gt-armed", "idle", now.isoformat(), now.isoformat(), armed_marker),
            (
                processing_id,
                "processing",
                "working",
                now.isoformat(),
                now.isoformat(),
                None,
            ),
            (idle_id, "normal-idle", "idle", now.isoformat(), now.isoformat(), None),
        ],
    )
    conn.commit()
    conn.close()

    def _fake_get_job(job_id):
        if job_id == f"golden-throne-{due_id}":
            return SimpleNamespace(next_run_time=datetime.now(UTC) - timedelta(seconds=5))
        if job_id == f"golden-throne-{armed_id}":
            return SimpleNamespace(next_run_time=datetime.now(UTC) + timedelta(minutes=5))
        return None

    monkeypatch.setattr(app_env.main.scheduler, "get_job", _fake_get_job)

    resp = client.get("/api/ui/ops/state")

    assert resp.status_code == 200, resp.text
    active = resp.json()["instances"]["active"]
    by_id = {item["id"]: item for item in active}
    assert [item["id"] for item in active] == [due_id, armed_id, processing_id, idle_id]
    assert by_id[due_id]["attention_rank"] == 4
    assert by_id[due_id]["attention_reasons"] == ["golden_throne_due"]
    assert by_id[armed_id]["attention_rank"] == 5
    assert by_id[armed_id]["attention_reasons"] == ["golden_throne_armed"]
    assert by_id[processing_id]["attention_rank"] == 6
    assert by_id[processing_id]["attention_reasons"] == ["processing_or_working"]
    assert by_id[idle_id]["attention_rank"] == 7
    assert by_id[idle_id]["attention_reasons"] == ["normal_idle"]


def test_ops_status_negative_break_balance_is_bad(client, app_env) -> None:
    app_env.main.timer_engine._break_balance_ms = -60_000

    resp = client.get("/api/ops/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["timer"]["is_in_backlog"] is True
    assert body["timer"]["break_backlog_ms"] == 60_000
    assert body["status"] == "bad"
    break_assertion = next(item for item in body["assertions"] if item["id"] == "break_balance")
    assert break_assertion["status"] == "bad"


def test_ops_status_pending_enforcement_recommends_action(client, app_env) -> None:
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


def test_ops_status_tmuxctld_health_unavailable_degrades_without_crash(client) -> None:
    resp = client.get("/api/ops/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sources"]["tmuxctld"]["status"] == "warn"
    assert body["sources"]["tmuxctld"]["available"] is False
    assert body["tmux"]["reachable"] is False
    assert body["tmux"]["tmux_reachable"] is None


def test_ops_timer_history_returns_live_shape(client, app_env) -> None:
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


def test_ops_timer_history_does_not_interpolate_sparse_shifts(client, app_env) -> None:
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


def test_ops_timer_history_marks_restart_sample_gap(client, app_env) -> None:
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


def test_ops_timer_history_flags_impossible_rate(client, app_env) -> None:
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


def test_ops_timer_history_marks_sparse_large_delta_as_gap(client, app_env) -> None:
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


def test_ops_timer_history_allows_reset_discontinuity(client, app_env) -> None:
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


def test_ops_timer_history_handles_missing_timer_samples_table(client, app_env) -> None:
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


def test_ops_timer_history_may_28_sparse_snap_regression(client, app_env) -> None:
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


def test_work_state_ignores_stale_idle_instances(client, app_env) -> None:
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


def test_work_action_sets_short_productivity_window(client) -> None:
    resp = client.post("/api/work-action", json={"source": "pytest", "note": "state assertion"})
    assert resp.status_code == 200, resp.text

    state_resp = client.get("/api/work-state")

    assert state_resp.status_code == 200, state_resp.text
    body = state_resp.json()
    assert body["productivity_active"] is True
    assert body["reason"] == "recent_work_action"


def test_ops_ui_serves_index_html(client) -> None:
    resp = client.get("/ui/ops")

    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    assert "Ops Cockpit" in resp.text


def test_ops_ui_serves_built_asset(client, app_env) -> None:
    ops_dir = Path(app_env.main.UI_DIR) / "ops"
    asset = next(ops_dir.glob("assets/*"), None)
    assert asset is not None, "run npm run build in token-api/web/ops before backend tests"

    rel = asset.relative_to(ops_dir).as_posix()
    resp = client.get(f"/ui/ops/{rel}")

    assert resp.status_code == 200, resp.text
    assert resp.content


def test_ops_ui_unknown_asset_returns_404(client) -> None:
    resp = client.get("/ui/ops/assets/does-not-exist.js")

    assert resp.status_code == 404


def test_ops_state_passes_through_tts_sender_metadata(client, app_env, monkeypatch) -> None:
    instance_id = _insert_ops_fixture(app_env)

    def _fake_tts_queue_status() -> dict:
        return {
            "current": {
                "instance_id": instance_id,
                "name": "ops-test",
                "message": "current line",
                "voice": "Microsoft David",
                "backend": "wsl",
                "playback_target": "wsl",
                "persona_slug": "ultramarines",
                "persona_display_name": "Ultramarines",
                "commander_type": "chapter",
                "started_at": "2026-07-09T10:00:00",
            },
            "hot_queue": [
                {
                    "instance_id": instance_id,
                    "name": "ops-test",
                    "message": "queued line",
                    "voice": "Microsoft David",
                    "playback_target": "phone",
                    "persona_slug": "ultramarines",
                    "persona_display_name": "Ultramarines",
                    "commander_type": "chapter",
                    "queue": "hot",
                    "queued_at": "2026-07-09T10:00:01",
                }
            ],
            "pause_queue": [],
            "hot_queue_length": 1,
            "pause_queue_length": 0,
            "queue_length": 1,
            "backend": "wsl",
            "satellite_available": True,
            "global_mode": "verbose",
            "routing": None,
        }

    monkeypatch.setattr(app_env.main, "get_tts_queue_status", _fake_tts_queue_status)

    resp = client.get("/api/ui/ops/state")

    assert resp.status_code == 200, resp.text
    tts = resp.json()["tts"]
    assert tts["current"]["persona_slug"] == "ultramarines"
    assert tts["current"]["persona_display_name"] == "Ultramarines"
    assert tts["current"]["commander_type"] == "chapter"
    assert tts["current"]["playback_target"] == "wsl"
    assert tts["hot_queue"][0]["persona_slug"] == "ultramarines"
    assert tts["hot_queue"][0]["playback_target"] == "phone"


# ── /api/ui/ops/session-docs — the Muster Ledger feed ────────────────────────
# First-ever coverage of the pipeline-board feed, pinned alongside the rubric +
# persona enrichment the cockpit kanban consumes. Docs are real files under the
# app_env vault root because the endpoint reads frontmatter from disk.


def _write_session_doc(app_env, name: str, frontmatter: str, body: str = "The work.\n") -> str:
    vault_root = Path(os.environ["IMPERIUM_ENV"])
    sessions = vault_root / "Sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    doc = sessions / f"{name}.md"
    doc.write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return f"Sessions/{name}.md"


def _insert_session_doc(
    app_env,
    file_path: str,
    *,
    title: str = "Muster Doc",
    status: str = "active",
    created_at: str = "2026-05-25T10:00:00",
) -> int:
    conn = sqlite3.connect(app_env.db_path)
    doc_id = conn.execute(
        """INSERT INTO session_documents
           (title, file_path, project, status, created_at, updated_at)
           VALUES (?, ?, 'token-os', ?, ?, ?)""",
        (title, file_path, status, created_at, created_at),
    ).lastrowid
    conn.commit()
    conn.close()
    return doc_id


def _get_docs(client) -> dict:
    resp = client.get("/api/ui/ops/session-docs")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_session_docs_rubric_summary_for_dict_rubric(client, app_env) -> None:
    rel = _write_session_doc(
        app_env,
        "rubric-dict",
        "victory:\n  a: true\n  b: false\n",
    )
    _insert_session_doc(app_env, rel)

    body = _get_docs(client)

    doc = body["docs"][0]
    rubric = doc["rubric"]
    assert rubric["present"] is True
    assert rubric["complete"] is False
    assert rubric["met"] == 1
    assert rubric["total"] == 2
    assert rubric["skipped"] == 0
    assert rubric["first_unmet"] == "b"
    assert rubric["notified_at"] is None
    assert rubric["acknowledged_at"] is None


def test_session_docs_no_rubric_reports_present_false(client, app_env) -> None:
    # evaluate_rubric treats a missing rubric as complete:true so legacy docs
    # never trip GT — the feed summary must still say present:false so the
    # cockpit never renders a victory state for a doc with no rubric at all.
    rel = _write_session_doc(app_env, "no-rubric", "project: token-os\n")
    _insert_session_doc(app_env, rel)

    body = _get_docs(client)

    rubric = body["docs"][0]["rubric"]
    assert rubric["present"] is False


def test_session_docs_legacy_scalar_rubric_is_well_formed(client, app_env) -> None:
    rel = _write_session_doc(app_env, "legacy-scalar", "victory: declared\n")
    _insert_session_doc(app_env, rel)

    body = _get_docs(client)

    rubric = body["docs"][0]["rubric"]
    assert rubric["present"] is True
    assert rubric["complete"] is True
    assert rubric["total"] == 1
    assert rubric["met"] == 1
    assert rubric["skipped"] == 0
    assert rubric["first_unmet"] is None


def test_session_docs_skip_counts_and_completes(client, app_env) -> None:
    rel = _write_session_doc(
        app_env,
        "rubric-skip",
        "victory:\n  a: true\n  b: false\nvictory_skip:\n  - b\n",
    )
    _insert_session_doc(app_env, rel)

    body = _get_docs(client)

    rubric = body["docs"][0]["rubric"]
    assert rubric["present"] is True
    assert rubric["complete"] is True
    assert rubric["met"] == 1
    assert rubric["total"] == 2
    assert rubric["skipped"] == 1
    assert rubric["first_unmet"] is None


def test_session_docs_persona_chip_for_known_slug(client, app_env) -> None:
    rel = _write_session_doc(app_env, "persona-known", "persona_slug: blood-angels\n")
    _insert_session_doc(app_env, rel)

    body = _get_docs(client)

    doc = body["docs"][0]
    assert doc["persona"]["slug"] == "blood-angels"
    assert doc["persona"]["chip_color"] == "#b1191e"
    assert doc["persona"]["display_name"] == "Blood Angels"
    # flat back-compat field stays
    assert doc["persona_slug"] == "blood-angels"


def test_session_docs_persona_unknown_or_absent_is_null_safe(client, app_env) -> None:
    rel_unknown = _write_session_doc(app_env, "persona-unknown", "persona_slug: not-a-legion\n")
    _insert_session_doc(app_env, rel_unknown, created_at="2026-05-25T11:00:00")
    rel_absent = _write_session_doc(app_env, "persona-absent", "project: token-os\n")
    _insert_session_doc(app_env, rel_absent, created_at="2026-05-25T10:00:00")

    body = _get_docs(client)

    unknown, absent = body["docs"][0], body["docs"][1]
    assert unknown["persona"]["slug"] == "not-a-legion"
    assert unknown["persona"]["chip_color"] is None
    assert absent["persona"]["slug"] is None
    assert absent["persona"]["chip_color"] is None


def test_session_docs_missing_file_falls_back_and_feed_survives(client, app_env) -> None:
    _insert_session_doc(app_env, "Sessions/never-written.md")

    body = _get_docs(client)

    doc = body["docs"][0]
    assert doc["rubric"]["present"] is False
    assert doc["persona"]["chip_color"] is None
    assert doc["head"] is None


def test_session_docs_spine_fields_unchanged(client, app_env) -> None:
    rel = _write_session_doc(
        app_env,
        "spine-doc",
        "victory:\n  a: true\npersona_slug: blood-angels\n",
    )
    doc_id = _insert_session_doc(app_env, rel, title="Spine Doc", status="active")
    instance_id = str(uuid.uuid4())
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO instances
           (id, name, working_dir, origin_type, device_id,
            status, engine, created_at, last_activity, session_doc_id)
           VALUES (?, 'spine-instance', '/tmp/ops', 'local', 'Mac-Mini',
                   'working', 'codex', '2026-05-25T10:00:00', '2026-05-25T10:01:00', ?)""",
        (instance_id, doc_id),
    )
    conn.commit()
    conn.close()

    body = _get_docs(client)

    assert body["lane_totals"] == {"active": 1}
    assert body["limit_per_lane"] == 12
    doc = body["docs"][0]
    assert doc["id"] == doc_id
    assert doc["title"] == "Spine Doc"
    assert doc["status"] == "active"
    assert doc["linked_instances"] == 1
    assert doc["session_date"] == "2026-05-25T10:00:00"
    assert doc["session_date_source"] == "db:created_at"
    assert doc["obsidian_uri"] is not None
