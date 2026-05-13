"""Tests for instance provenance logging and reconciliation surfaces."""

import sqlite3
import sys
import uuid

import pytest


@pytest.fixture
def client(app_env):
    from fastapi.testclient import TestClient

    return TestClient(app_env.main.app)


def _db(app_env):
    conn = sqlite3.connect(app_env.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _session_start(client, *, tmux_pane=None):
    instance_id = str(uuid.uuid4())
    payload = {
        "session_id": instance_id,
        "cwd": f"/tmp/{instance_id}",
        "pid": 12345,
    }
    if tmux_pane is not None:
        payload["tmux_pane"] = tmux_pane
    resp = client.post("/api/hooks/SessionStart", json=payload)
    assert resp.status_code == 200, resp.text
    return instance_id


def _mutations_for(app_env, instance_id):
    conn = _db(app_env)
    rows = conn.execute(
        "SELECT * FROM instance_mutations WHERE instance_id = ? ORDER BY id ASC",
        (instance_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


class TestSchema:
    def test_instance_mutations_table_and_indexes_exist(self, app_env):
        conn = _db(app_env)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='instance_mutations'"
        ).fetchall()
        conn.close()

        table_names = {row[0] for row in tables}
        index_names = {row[0] for row in indexes}
        assert "instance_mutations" in table_names
        assert "idx_instance_mutations_instance_time" in index_names
        assert "idx_instance_mutations_write_txn" in index_names
        assert "idx_instance_mutations_type_time" in index_names


class TestProvenance:
    def test_session_start_writes_sanctioned_mutation(self, client, app_env):
        instance_id = _session_start(client)
        rows = _mutations_for(app_env, instance_id)
        assert rows
        assert rows[0]["mutation_type"] == "instance_registered"
        assert rows[0]["write_source"] == "hooks"
        assert rows[0]["actor"] == "SessionStart"

    def test_session_start_reactivates_existing_stopped_codex_row(
        self, client, app_env, monkeypatch
    ):
        instance_id = str(uuid.uuid4())
        conn = _db(app_env)
        conn.execute(
            """INSERT INTO claude_instances
               (id, session_id, tab_name, working_dir, origin_type, device_id,
                status, synced, tmux_pane, pane_label, engine, stopped_at,
                registered_at, last_activity)
               VALUES (?, ?, 'stale codex', '/tmp/old', 'local', 'Mac-Mini',
                       'stopped', 0, '%old', 'somnium:NW', 'codex',
                       '2026-01-01T00:00:00',
                       '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
            (instance_id, str(uuid.uuid4())),
        )
        conn.commit()
        conn.close()

        async def pane_label(pane):
            assert pane == "%new"
            return "somnium:NE"

        monkeypatch.setattr(sys.modules["routes.hooks"], "_tmux_pane_label", pane_label)

        resp = client.post(
            "/api/hooks/SessionStart",
            json={
                "session_id": instance_id,
                "cwd": "/Volumes/Imperium/Imperium-ENV",
                "pid": 4242,
                "tmux_pane": "%new",
                "env": {
                    "TOKEN_API_ENGINE": "codex",
                    "TOKEN_API_LAUNCHER": "codex-dispatch",
                    "TOKEN_API_WRAPPER_LAUNCH_ID": "bridge-1",
                },
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["action"] == "reregistered"

        conn = _db(app_env)
        row = conn.execute(
            """SELECT status, stopped_at, tmux_pane, pane_label, working_dir,
                      pid, engine, launcher, wrapper_launch_id
               FROM claude_instances WHERE id = ?""",
            (instance_id,),
        ).fetchone()
        conn.close()

        assert row["status"] == "idle"
        assert row["stopped_at"] is None
        assert row["tmux_pane"] == "%new"
        assert row["pane_label"] == "somnium:NE"
        assert row["working_dir"] == "/Volumes/Imperium/Imperium-ENV"
        assert row["pid"] == 4242
        assert row["engine"] == "codex"
        assert row["launcher"] == "codex-dispatch"
        assert row["wrapper_launch_id"] == "bridge-1"


    def test_session_start_records_dispatch_discord_metadata(self, client, app_env):
        instance_id = str(uuid.uuid4())
        resp = client.post(
            "/api/hooks/SessionStart",
            json={
                "session_id": instance_id,
                "cwd": "/tmp/dispatch-meta",
                "pid": 777,
                "env": {
                    "TOKEN_API_LAUNCHER": "dispatch",
                    "TOKEN_API_ENGINE": "codex",
                    "TOKEN_API_WRAPPER_LAUNCH_ID": "dispatch-bridge-1",
                    "TOKEN_API_DISPATCH_TARGET": "legion:new",
                    "TOKEN_API_DISPATCH_WINDOW": "legion",
                    "TOKEN_API_DISPATCH_MODE": "new",
                    "TOKEN_API_DISPATCH_SLOT": "new",
                    "TOKEN_API_DISPATCH_SESSION_DOC_PATH": "Mars/Sessions/test.md",
                    "TOKEN_API_TARGET_WORKING_DIR": "/tmp/dispatch-meta",
                    "TOKEN_API_LAUNCH_MODE": "tmux_stack_new",
                    "TOKEN_API_INSTANCE_TYPE": "golden_throne",
                    "TOKEN_API_ZEALOTRY": "7",
                    "TOKEN_API_DISCORD_HOSTED": "1",
                    "TOKEN_API_DISCORD_CHANNEL": "1234567890",
                    "TOKEN_API_DISCORD_BOT": "mechanicus",
                },
            },
        )
        assert resp.status_code == 200, resp.text

        conn = _db(app_env)
        row = conn.execute(
            """SELECT launcher, engine, wrapper_launch_id, dispatch_target,
                      dispatch_window, dispatch_mode, dispatch_slot,
                      dispatch_session_doc_path, target_working_dir, launch_mode,
                      instance_type, zealotry, discord_hosted, discord_channel, discord_bot
               FROM claude_instances WHERE id = ?""",
            (instance_id,),
        ).fetchone()
        conn.close()

        assert row["launcher"] == "dispatch"
        assert row["engine"] == "codex"
        assert row["wrapper_launch_id"] == "dispatch-bridge-1"
        assert row["dispatch_target"] == "legion:new"
        assert row["dispatch_window"] == "legion"
        assert row["dispatch_mode"] == "new"
        assert row["dispatch_slot"] == "new"
        assert row["dispatch_session_doc_path"] == "Mars/Sessions/test.md"
        assert row["target_working_dir"] == "/tmp/dispatch-meta"
        assert row["launch_mode"] == "tmux_stack_new"
        assert row["instance_type"] == "golden_throne"
        assert row["zealotry"] == 7
        assert row["discord_hosted"] == 1
        assert row["discord_channel"] == "1234567890"
        assert row["discord_bot"] == "mechanicus"

    def test_manual_assign_doc_writes_continuity_mutation(self, client, app_env):
        instance_id = _session_start(client)
        conn = _db(app_env)
        conn.execute(
            """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
               VALUES ('Doc', '/tmp/doc.md', 'proj', 'active', datetime('now'), datetime('now'))"""
        )
        doc_id = conn.execute("SELECT id FROM session_documents").fetchone()[0]
        conn.commit()
        conn.close()

        resp = client.post(f"/api/instances/{instance_id}/assign-doc", params={"doc_id": doc_id})
        assert resp.status_code == 200, resp.text

        rows = _mutations_for(app_env, instance_id)
        latest = rows[-1]
        assert latest["mutation_type"] == "continuity_binding_changed"
        assert latest["write_source"] == "api"
        assert latest["actor"] == "assign-doc"


class TestReconciliation:
    def test_clean_instance_returns_clean(self, client):
        instance_id = _session_start(client)
        resp = client.get(f"/api/instances/{instance_id}/reconciliation")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "clean"

    def test_direct_sql_write_is_unprovenanced(self, client, app_env):
        instance_id = _session_start(client)
        conn = _db(app_env)
        conn.execute(
            "UPDATE claude_instances SET legion = 'mechanicus' WHERE id = ?", (instance_id,)
        )
        conn.commit()
        conn.close()

        resp = client.get(f"/api/instances/{instance_id}/reconciliation")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "unprovenanced_write"

    def test_pending_projection_detected_from_queue(self, client):
        instance_id = _session_start(client, tmux_pane="%99")
        resp = client.post(
            f"/api/instances/{instance_id}/activity", json={"action": "prompt_submit"}
        )
        assert resp.status_code == 200, resp.text

        resp = client.get(f"/api/instances/{instance_id}/reconciliation")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "pending_projection"

    def test_rename_is_provenanced_and_reconciles_clean(self, client, app_env):
        instance_id = _session_start(client)
        resp = client.patch(f"/api/instances/{instance_id}/rename", json={"tab_name": "Fresh Name"})
        assert resp.status_code == 200, resp.text

        latest = _mutations_for(app_env, instance_id)[-1]
        assert latest["mutation_type"] == "instance_updated"
        assert latest["actor"] == "rename-instance"

        resp = client.get(f"/api/instances/{instance_id}/reconciliation")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "clean"

    def test_hard_delete_session_doc_writes_continuity_mutation(self, client, app_env):
        instance_id = _session_start(client)
        conn = _db(app_env)
        conn.execute(
            """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
               VALUES ('Doc', ?, 'proj', 'active', datetime('now'), datetime('now'))""",
            (str(app_env.db_path.parent / "doc.md"),),
        )
        doc_id = conn.execute("SELECT id FROM session_documents").fetchone()[0]
        conn.commit()
        conn.close()

        resp = client.post(f"/api/instances/{instance_id}/assign-doc", params={"doc_id": doc_id})
        assert resp.status_code == 200, resp.text
        resp = client.delete(f"/api/session-docs/{doc_id}", params={"hard": "true"})
        assert resp.status_code == 200, resp.text

        latest = _mutations_for(app_env, instance_id)[-1]
        assert latest["mutation_type"] == "continuity_binding_changed"
        assert latest["actor"] == "delete-session-doc"

    def test_global_tts_mode_fanout_is_provenanced(self, client, app_env):
        first = _session_start(client)
        second = _session_start(client)

        resp = client.post("/api/tts/global-mode", json={"mode": "silent"})
        assert resp.status_code == 200, resp.text

        first_latest = _mutations_for(app_env, first)[-1]
        second_latest = _mutations_for(app_env, second)[-1]
        assert first_latest["actor"] == "tts-global-mode"
        assert second_latest["actor"] == "tts-global-mode"
        assert first_latest["mutation_type"] == "instance_updated"
        assert second_latest["mutation_type"] == "instance_updated"

    def test_stop_hook_marking_is_provenanced(self, client, app_env):
        instance_id = _session_start(client)

        from stop_hook import mark_cron_instance_stopped

        mark_cron_instance_stopped(instance_id)

        latest = _mutations_for(app_env, instance_id)[-1]
        assert latest["mutation_type"] == "instance_stopped"
        assert latest["actor"] == "stop-hook"

    def test_primarch_supplant_clears_old_legion_pane_tint(self, client, app_env):
        old_id = str(uuid.uuid4())
        new_id = str(uuid.uuid4())
        conn = _db(app_env)
        conn.execute(
            """INSERT INTO claude_instances
               (id, session_id, tab_name, working_dir, origin_type, device_id,
                status, legion, synced, tmux_pane, primarch, registered_at, last_activity)
               VALUES (?, ?, 'old-custodes', '/tmp/old', 'local', 'Mac-Mini',
                       'idle', 'custodes', 1, '%old', 'custodes',
                       datetime('now'), datetime('now'))""",
            (old_id, str(uuid.uuid4())),
        )
        conn.commit()
        conn.close()

        resp = client.post(
            "/api/hooks/SessionStart",
            json={
                "session_id": new_id,
                "cwd": "/tmp/new",
                "pid": 12345,
                "tmux_pane": "%new",
                "env": {"TOKEN_API_PRIMARCH": "custodes"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["action"] == "supplanted"

        conn = _db(app_env)
        rows = conn.execute(
            "SELECT legion, tmux_pane FROM pane_recolor_queue ORDER BY id ASC"
        ).fetchall()
        conn.close()

        assert [tuple(row) for row in rows] == [
            ("custodes", "%new"),
            ("astartes", "%old"),
        ]
