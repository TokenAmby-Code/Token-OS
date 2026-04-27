"""Tests for legion-aware Discord routing and synced sessions.

Covers:
- Schema: legion + synced columns on claude_instances
- API: PATCH legion, PATCH synced (one-per-legion), GET synced-session
- Helpers: _format_discord_injection
- Cleanup: synced=0 on stop
- Auto-detect: civic legion from working_dir
- Morning ack: Discord keyword triggers acknowledge
- Cron: legion column on cron_jobs

Uses a temporary SQLite database via TOKEN_API_DB env var.
"""

import asyncio
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ALLOWED_LEGIONS = set()
MORNING_ENFORCE_STATE = {}
_format_discord_injection = None
_TEST_DB_PATH = None


@pytest.fixture
def client(app_env):
    """Create a test client for the FastAPI app."""
    from fastapi.testclient import TestClient

    return TestClient(app_env.main.app)


@pytest.fixture(autouse=True)
def _bind_main_globals(app_env):
    global ALLOWED_LEGIONS, MORNING_ENFORCE_STATE, _format_discord_injection, _TEST_DB_PATH
    ALLOWED_LEGIONS = app_env.main.ALLOWED_LEGIONS
    MORNING_ENFORCE_STATE = app_env.main.MORNING_ENFORCE_STATE
    _format_discord_injection = app_env.main._format_discord_injection
    _TEST_DB_PATH = str(app_env.db_path)


def _insert_instance(
    instance_id=None,
    *,
    legion="astartes",
    synced=0,
    status="idle",
    tmux_pane=None,
    working_dir="/tmp",
    last_activity=None,
    db_path=None,
):
    """Insert a minimal test instance directly into DB."""
    iid = instance_id or str(uuid.uuid4())
    now = last_activity or datetime.now().isoformat()
    conn = sqlite3.connect(db_path or _TEST_DB_PATH)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, legion, synced, tmux_pane, registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', ?, ?, ?, ?, ?, ?)""",
        (
            iid,
            str(uuid.uuid4()),
            f"test-{iid[:8]}",
            working_dir,
            status,
            legion,
            synced,
            tmux_pane,
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()
    return iid


def _get_instance(instance_id):
    """Read an instance row from DB."""
    conn = sqlite3.connect(_TEST_DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM claude_instances WHERE id = ?", (instance_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _get_workflow_events(instance_id):
    """Read workflow events for an instance directly from DB."""
    conn = sqlite3.connect(_TEST_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM workflow_events WHERE instance_id = ? ORDER BY id ASC",
        (instance_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ── 1. Schema Tests ──────────────────────────────────────────


class TestSchema:
    def test_legion_column_defaults_astartes(self):
        iid = _insert_instance()
        row = _get_instance(iid)
        assert row["legion"] == "astartes"

    def test_synced_column_defaults_zero(self):
        iid = _insert_instance()
        row = _get_instance(iid)
        assert row["synced"] == 0

    def test_legion_synced_index_exists(self):
        conn = sqlite3.connect(_TEST_DB_PATH)
        indices = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='claude_instances'"
        ).fetchall()
        conn.close()
        index_names = {row[0] for row in indices}
        assert "idx_instances_legion_synced" in index_names

    def test_workflow_columns_exist(self):
        conn = sqlite3.connect(_TEST_DB_PATH)
        cols = conn.execute("PRAGMA table_info(claude_instances)").fetchall()
        conn.close()
        names = {row[1] for row in cols}
        assert {
            "continuity_binding_source",
            "workflow_state",
            "stop_allowed",
            "next_required_action",
        } <= names

    def test_workflow_events_table_exists(self):
        conn = sqlite3.connect(_TEST_DB_PATH)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        indices = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='workflow_events'"
        ).fetchall()
        conn.close()
        table_names = {row[0] for row in tables}
        index_names = {row[0] for row in indices}
        assert "workflow_events" in table_names
        assert "idx_workflow_events_instance_time" in index_names
        assert "idx_workflow_events_type_time" in index_names


# ── 2. PATCH /api/instances/{id}/legion ──────────────────────


class TestSetLegion:
    def test_set_legion_valid(self, client):
        for legion in ALLOWED_LEGIONS:
            iid = _insert_instance()
            resp = client.patch(f"/api/instances/{iid}/legion", json={"legion": legion})
            assert resp.status_code == 200
            assert resp.json()["legion"] == legion
            assert _get_instance(iid)["legion"] == legion

    def test_set_legion_invalid(self, client):
        iid = _insert_instance()
        resp = client.patch(f"/api/instances/{iid}/legion", json={"legion": "unknown"})
        assert resp.status_code == 400

    def test_set_legion_not_found(self, client):
        resp = client.patch("/api/instances/nonexistent-id/legion", json={"legion": "custodes"})
        assert resp.status_code == 404


# ── 3. PATCH /api/instances/{id}/synced ──────────────────────


class TestSetSynced:
    def test_set_synced_true(self, client):
        iid = _insert_instance(legion="custodes")
        resp = client.patch(f"/api/instances/{iid}/synced", json={"synced": True})
        assert resp.status_code == 200
        assert resp.json()["synced"] is True
        assert _get_instance(iid)["synced"] == 1

    def test_set_synced_false(self, client):
        iid = _insert_instance(legion="custodes", synced=1)
        resp = client.patch(f"/api/instances/{iid}/synced", json={"synced": False})
        assert resp.status_code == 200
        assert resp.json()["synced"] is False
        assert _get_instance(iid)["synced"] == 0

    def test_synced_one_per_legion(self, client):
        """Second synced=true in same legion should 409."""
        iid1 = _insert_instance(legion="custodes")
        iid2 = _insert_instance(legion="custodes")

        resp1 = client.patch(f"/api/instances/{iid1}/synced", json={"synced": True})
        assert resp1.status_code == 200

        resp2 = client.patch(f"/api/instances/{iid2}/synced", json={"synced": True})
        assert resp2.status_code == 409

    def test_synced_different_legions(self, client):
        """Different legions can each have a synced session."""
        iid1 = _insert_instance(legion="custodes")
        iid2 = _insert_instance(legion="mechanicus")

        resp1 = client.patch(f"/api/instances/{iid1}/synced", json={"synced": True})
        assert resp1.status_code == 200

        resp2 = client.patch(f"/api/instances/{iid2}/synced", json={"synced": True})
        assert resp2.status_code == 200

    def test_synced_stopped_no_conflict(self, client):
        """Stopped instance with synced=1 shouldn't block new synced."""
        iid1 = _insert_instance(legion="custodes", synced=1, status="stopped")
        iid2 = _insert_instance(legion="custodes")

        resp = client.patch(f"/api/instances/{iid2}/synced", json={"synced": True})
        assert resp.status_code == 200

    def test_set_synced_not_found(self, client):
        resp = client.patch("/api/instances/nonexistent/synced", json={"synced": True})
        assert resp.status_code == 404


# ── 4. GET /api/legion/{legion}/synced-session ───────────────


class TestSyncedSessionLookup:
    def test_synced_session_found(self, client):
        iid = _insert_instance(legion="custodes", synced=1, tmux_pane="%5")
        resp = client.get("/api/legion/custodes/synced-session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["synced_session"] is not None
        assert data["synced_session"]["id"] == iid

    def test_synced_session_none(self, client):
        resp = client.get("/api/legion/custodes/synced-session")
        assert resp.status_code == 200
        assert resp.json()["synced_session"] is None

    def test_synced_session_stopped_excluded(self, client):
        _insert_instance(legion="custodes", synced=1, status="stopped")
        resp = client.get("/api/legion/custodes/synced-session")
        assert resp.status_code == 200
        assert resp.json()["synced_session"] is None

    def test_synced_session_invalid_legion(self, client):
        resp = client.get("/api/legion/invalid/synced-session")
        assert resp.status_code == 400


# ── 5. _format_discord_injection (unit tests) ────────────────


class TestFormatDiscordInjection:
    def test_strips_user_mentions(self):
        result = _format_discord_injection("chat", "<@123456> hello world")
        assert result == "[Emperor via Discord #chat]: hello world"

    def test_strips_role_mentions(self):
        result = _format_discord_injection("fleet", "<@&789> deploy now")
        assert result == "[Emperor via Discord #fleet]: deploy now"

    def test_strips_multiple_mentions(self):
        result = _format_discord_injection("chat", "<@123> <@&456> hey")
        assert result == "[Emperor via Discord #chat]: hey"

    def test_empty_content(self):
        result = _format_discord_injection("dm", "")
        assert result == "[Emperor via Discord #dm]: "

    def test_no_mentions(self):
        result = _format_discord_injection("general", "just a message")
        assert result == "[Emperor via Discord #general]: just a message"


# ── 6. Synced cleanup on stop ────────────────────────────────


class TestSyncedCleanupOnStop:
    def test_stop_clears_synced(self, client):
        """DELETE instance with synced=1 should set synced=0."""
        iid = _insert_instance(legion="custodes", synced=1)
        resp = client.delete(f"/api/instances/{iid}")
        assert resp.status_code == 200
        row = _get_instance(iid)
        assert row["synced"] == 0
        assert row["status"] == "stopped"

    def test_stale_cleanup_clears_synced(self):
        """Stale instance cleanup should set synced=0."""
        # Insert instance with old last_activity (4 hours ago)
        old_time = (datetime.now() - timedelta(hours=4)).isoformat()
        iid = _insert_instance(legion="mechanicus", synced=1, last_activity=old_time)

        # Run cleanup
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(__import__("main").cleanup_stale_instances())
        loop.close()

        assert result["cleaned_up"] >= 1
        row = _get_instance(iid)
        assert row["synced"] == 0
        assert row["status"] == "stopped"


# ── 7. Civic auto-detect (hook registration) ─────────────────


class TestCivicAutoDetect:
    def _register_via_hook(self, client, working_dir="/tmp/test", session_id=None):
        """Register an instance via the SessionStart hook endpoint."""
        sid = session_id or str(uuid.uuid4())
        payload = {
            "session_id": sid,
            "cwd": working_dir,
            "env": {},
            "pid": 99999,
        }
        resp = client.post("/api/hooks/SessionStart", json=payload)
        assert resp.status_code == 200, f"Hook failed: {resp.text}"
        return resp.json()

    def test_civic_autodetect_pax_env(self, client):
        sid = str(uuid.uuid4())
        self._register_via_hook(client, working_dir="/Volumes/Imperium/Pax-ENV", session_id=sid)
        row = _get_instance(sid)
        assert row is not None
        assert row["legion"] == "civic"

    def test_civic_autodetect_pax_path(self, client):
        sid = str(uuid.uuid4())
        self._register_via_hook(client, working_dir="/mnt/imperium/pax/project", session_id=sid)
        row = _get_instance(sid)
        assert row is not None
        assert row["legion"] == "civic"

    def test_no_autodetect_normal_dir(self, client):
        sid = str(uuid.uuid4())
        self._register_via_hook(
            client, working_dir="/Volumes/Imperium/Imperium-ENV", session_id=sid
        )
        row = _get_instance(sid)
        assert row is not None
        assert row["legion"] == "astartes"

    def test_cron_autodetect_mechanicus(self, client):
        """Cron origin should auto-detect as mechanicus."""
        sid = str(uuid.uuid4())
        payload = {
            "session_id": sid,
            "cwd": "/Volumes/Imperium/Imperium-ENV",
            "env": {"CRON_JOB_NAME": "test-job", "CRON_JOB_ID": "test-123"},
            "pid": 99999,
        }
        resp = client.post("/api/hooks/SessionStart", json=payload)
        assert resp.status_code == 200
        row = _get_instance(sid)
        assert row is not None
        assert row["legion"] == "mechanicus"


# ── 8. Morning ack via Discord ────────────────────────────────


class TestMorningAckViaDiscord:
    def _post_discord_message(self, client, content, channel="chat"):
        return client.post(
            "/api/discord/message",
            json={
                "channel_id": "test-channel-id",
                "channel_name": channel,
                "content": content,
                "author": {"username": "Emperor", "id": "12345"},
            },
        )

    def test_discord_ack_clears_enforce(self, client):
        """'ack' keyword in Discord should clear pending enforce state."""
        # Set enforce to pending
        MORNING_ENFORCE_STATE.update(
            {
                "status": "pending",
                "session_type": "morning_session",
                "fired_at": datetime.utcnow().isoformat(),
                "acknowledged_at": None,
                "override_reason": None,
                "escalation_level": 0,
            }
        )

        resp = self._post_discord_message(client, "ack")
        assert resp.status_code == 200
        assert MORNING_ENFORCE_STATE["status"] == "acknowledged"

    def test_discord_ack_keywords(self, client):
        """All ack keywords should work."""
        for keyword in ("ack", "acknowledged", "acknowledge", "here", "awake"):
            MORNING_ENFORCE_STATE.update(
                {
                    "status": "pending",
                    "session_type": "morning_session",
                    "fired_at": datetime.utcnow().isoformat(),
                    "acknowledged_at": None,
                    "escalation_level": 0,
                }
            )
            self._post_discord_message(client, keyword)
            assert MORNING_ENFORCE_STATE["status"] == "acknowledged", f"Keyword '{keyword}' failed"

    def test_discord_no_ack_when_idle(self, client):
        """'ack' when enforce is idle should not change state."""
        MORNING_ENFORCE_STATE["status"] = "idle"
        self._post_discord_message(client, "ack")
        assert MORNING_ENFORCE_STATE["status"] == "idle"

    def test_discord_ack_case_insensitive(self, client):
        """Ack keywords should be case-insensitive."""
        MORNING_ENFORCE_STATE.update(
            {
                "status": "pending",
                "session_type": "morning_session",
                "fired_at": datetime.utcnow().isoformat(),
                "acknowledged_at": None,
                "escalation_level": 0,
            }
        )
        self._post_discord_message(client, "ACK")
        assert MORNING_ENFORCE_STATE["status"] == "acknowledged"


# ── 9. Workflow / continuity state ───────────────────────────


class TestWorkflowState:
    def test_dispatch_start_persists_binding_and_events(self, client):
        sid = str(uuid.uuid4())
        session_doc = Path(tempfile.mkdtemp()) / "dispatch-note.md"
        session_doc.write_text(
            "---\ntitle: Dispatch Note\ntype: session\nstatus: active\n---\n\n# Dispatch\n",
            encoding="utf-8",
        )

        resp = client.post(
            "/api/hooks/SessionStart",
            json={
                "session_id": sid,
                "cwd": "/Volumes/Imperium/Imperium-ENV",
                "pid": 12345,
                "env": {
                    "TOKEN_API_ENGINE": "claude",
                    "TOKEN_API_LAUNCHER": "vault-dispatch",
                    "TOKEN_API_DISPATCH_TARGET": "legion:new",
                    "TOKEN_API_DISPATCH_WINDOW": "legion",
                    "TOKEN_API_DISPATCH_MODE": "stack_new",
                    "TOKEN_API_DISPATCH_SESSION_DOC_PATH": str(session_doc),
                    "TOKEN_API_TARGET_WORKING_DIR": "/Volumes/Imperium/Token-OS",
                    "TOKEN_API_LAUNCH_MODE": "vault_then_transplant",
                    "TOKEN_API_TRANSPLANT_EXPECTED": "true",
                },
            },
        )
        assert resp.status_code == 200, resp.text

        row = _get_instance(sid)
        assert row["session_doc_id"] is not None
        assert row["session_doc_policy"] == "dispatch_explicit"
        assert row["continuity_binding_source"] == "dispatch"
        assert row["workflow_state"] == "dispatching"
        assert row["stop_allowed"] == 1

        events = _get_workflow_events(sid)
        event_types = [event["event_type"] for event in events]
        assert "session_doc_bound" in event_types
        assert "continuity_binding_changed" in event_types
        assert "workflow_state_changed" in event_types

    def test_codex_direct_target_starts_in_worktree(self, client):
        sid = str(uuid.uuid4())
        session_doc = Path(tempfile.mkdtemp()) / "codex-dispatch.md"
        session_doc.write_text(
            "---\ntitle: Codex Dispatch\ntype: session\nstatus: active\n---\n\n# Dispatch\n",
            encoding="utf-8",
        )

        resp = client.post(
            "/api/hooks/SessionStart",
            json={
                "session_id": sid,
                "cwd": "/Volumes/Imperium/Token-OS",
                "pid": 22222,
                "env": {
                    "TOKEN_API_ENGINE": "codex",
                    "TOKEN_API_LAUNCHER": "vault-dispatch",
                    "TOKEN_API_DISPATCH_TARGET": "bridge:BL",
                    "TOKEN_API_DISPATCH_WINDOW": "bridge",
                    "TOKEN_API_DISPATCH_MODE": "named_slot",
                    "TOKEN_API_DISPATCH_SLOT": "BL",
                    "TOKEN_API_DISPATCH_SESSION_DOC_PATH": str(session_doc),
                    "TOKEN_API_TARGET_WORKING_DIR": "/Volumes/Imperium/Token-OS",
                    "TOKEN_API_LAUNCH_MODE": "direct_target",
                    "TOKEN_API_TRANSPLANT_EXPECTED": "false",
                },
            },
        )
        assert resp.status_code == 200, resp.text

        row = _get_instance(sid)
        assert row["workflow_state"] == "worktree"
        assert row["continuity_binding_source"] == "dispatch"

    def test_workflow_events_endpoint_returns_recent_events(self, client):
        iid = _insert_instance()
        conn = sqlite3.connect(_TEST_DB_PATH)
        conn.execute(
            """INSERT INTO workflow_events (instance_id, workflow_state, event_type, event_owner, details_json)
               VALUES (?, ?, ?, ?, ?)""",
            (
                iid,
                "worktree",
                "workflow_state_changed",
                "test",
                '{"new_workflow_state":"worktree"}',
            ),
        )
        conn.commit()
        conn.close()

        resp = client.get(f"/api/instances/{iid}/workflow-events")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["event_type"] == "workflow_state_changed"
        assert data[0]["details"]["new_workflow_state"] == "worktree"

    def test_stop_validate_blocks_and_sets_workflow(self, client):
        sid = _insert_instance(status="idle")
        conn = sqlite3.connect(_TEST_DB_PATH)
        conn.execute(
            """UPDATE claude_instances
               SET instance_type = 'golden_throne',
                   workflow_state = 'worktree',
                   stop_allowed = 1
               WHERE id = ?""",
            (sid,),
        )
        conn.commit()
        conn.close()

        resp = client.post("/api/hooks/StopValidate", json={"session_id": sid})
        assert resp.status_code == 200
        assert resp.json()["decision"] == "block"

        row = _get_instance(sid)
        assert row["workflow_state"] == "blocked"
        assert row["workflow_blocked_reason"] == "self_eval_required"
        assert row["stop_allowed"] == 0
        assert row["next_required_action"] == "self_eval"
        assert row["next_action_owner"] == "agent"

        events = _get_workflow_events(sid)
        event_types = [event["event_type"] for event in events]
        assert "stop_blocked" in event_types
        assert "workflow_state_changed" in event_types


# ── 9. Cron engine legion column ──────────────────────────────


class TestCronEngineLegion:
    def test_cron_jobs_legion_column(self):
        """cron_jobs table should have a legion column defaulting to 'mechanicus'."""
        import aiosqlite

        from cron_engine import CronEngine

        async def _check():
            async with aiosqlite.connect(_TEST_DB_PATH) as db:
                await CronEngine.init_tables(db)
                await db.commit()
                cursor = await db.execute("PRAGMA table_info(cron_jobs)")
                columns = {row[1]: row[4] for row in await cursor.fetchall()}
                assert "legion" in columns
                assert columns["legion"] == "'mechanicus'"

        loop = asyncio.new_event_loop()
        loop.run_until_complete(_check())
        loop.close()
