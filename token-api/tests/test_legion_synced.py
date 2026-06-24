"""Tests for legion-aware Discord routing and synced sessions.

Covers:
- Schema: legion + synced columns on legacy_instances
- API: PATCH legion, PATCH synced (one-per-legion), GET synced-session
- Helpers: _format_discord_injection
- Cleanup: synced=0 on stop
- Auto-detect: civic legion from working_dir
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
from fastapi.testclient import TestClient

ALLOWED_LEGIONS = set()
_format_discord_injection = None
_TEST_DB_PATH = None


@pytest.fixture
def client(app_env):
    """Create a test client for the FastAPI app."""
    from fastapi.testclient import TestClient

    return TestClient(app_env.main.app)


@pytest.fixture(autouse=True)
def _bind_main_globals(app_env):
    global ALLOWED_LEGIONS, _format_discord_injection, _TEST_DB_PATH
    ALLOWED_LEGIONS = app_env.main.ALLOWED_LEGIONS
    _format_discord_injection = app_env.main._format_discord_injection
    _TEST_DB_PATH = str(app_env.db_path)


def _insert_instance(
    instance_id=None,
    *,
    legion="astartes",
    synced=0,
    status="idle",
    working_dir="/tmp",
    last_activity=None,
    db_path=None,
):
    """Insert a minimal test instance directly into DB."""
    iid = instance_id or str(uuid.uuid4())
    now = last_activity or datetime.now().isoformat()
    conn = sqlite3.connect(db_path or _TEST_DB_PATH)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, legion, synced, registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', ?, ?, ?, ?, ?)""",
        (
            iid,
            str(uuid.uuid4()),
            f"test-{iid[:8]}",
            working_dir,
            status,
            legion,
            synced,
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
    row = conn.execute("SELECT * FROM legacy_instances WHERE id = ?", (instance_id,)).fetchone()
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
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='instances'"
        ).fetchall()
        conn.close()
        index_names = {row[0] for row in indices}
        assert "idx_instances_gt" in index_names

    def test_workflow_columns_exist(self):
        conn = sqlite3.connect(_TEST_DB_PATH)
        cols = conn.execute("PRAGMA table_info(legacy_instances)").fetchall()
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
            conn = sqlite3.connect(_TEST_DB_PATH)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT p.slug AS persona_slug
                   FROM instances i LEFT JOIN personas p ON p.id = i.persona_id
                   WHERE i.id = ?""",
                (iid,),
            ).fetchone()
            conn.close()
            if legion == "custodes":
                assert row["persona_slug"] == "custodes"
            elif legion == "fabricator":
                assert row["persona_slug"] == "fabricator-general"
            elif legion == "civic":
                assert row["persona_slug"] is None

    def test_set_legion_singleton_updates_canonical_persona_tint(
        self, client, app_env, monkeypatch
    ):
        import personas

        tint_calls = []

        async def _pane(_instance_id):
            return ("%cust", "main")

        monkeypatch.setattr(app_env.main.shared, "resolve_instance_pane", _pane)
        monkeypatch.setattr(
            app_env.main.shared,
            "apply_pane_tint",
            lambda pane, pane_tint, **kw: tint_calls.append((pane, pane_tint)),
        )

        iid = _insert_instance()
        resp = client.patch(f"/api/instances/{iid}/legion", json={"legion": "custodes"})
        assert resp.status_code == 200

        row = _get_instance(iid)
        assert row["profile_name"] == "custodes"
        assert row["tts_voice"] == "Microsoft George"
        assert ("%cust", "#302800") in tint_calls

        conn = sqlite3.connect(app_env.db_path)
        persona_id = conn.execute(
            "SELECT persona_id FROM instances WHERE id = ?", (iid,)
        ).fetchone()[0]
        conn.close()
        assert persona_id == personas.persona_id_for_slug("custodes")

    def test_set_legion_civic_preserves_canonical_persona_tint(
        self, client, app_env, monkeypatch
    ) -> None:
        import personas

        tint_calls = []

        async def _pane(_instance_id):
            return ("%pax", "main")

        monkeypatch.setattr(app_env.main.shared, "resolve_instance_pane", _pane)
        monkeypatch.setattr(
            app_env.main.shared,
            "apply_pane_tint",
            lambda pane, pane_tint, **kw: tint_calls.append((pane, pane_tint)),
        )

        iid = _insert_instance()
        from instance_mutation import sanctioned_update_instance_sync

        persona_id = personas.persona_id_for_slug("ultramarines")
        conn = sqlite3.connect(app_env.db_path)
        expected_tint = conn.execute(
            "SELECT pane_tint FROM personas WHERE id = ?", (persona_id,)
        ).fetchone()[0]
        sanctioned_update_instance_sync(
            conn,
            instance_id=iid,
            updates={"persona_id": persona_id},
            mutation_type="test_persona_assignment",
            write_source="test",
            actor="test_set_legion_civic_preserves_canonical_persona_tint",
        )
        conn.commit()
        conn.close()

        resp = client.patch(f"/api/instances/{iid}/legion", json={"legion": "civic"})
        assert resp.status_code == 200
        assert ("%pax", expected_tint) in tint_calls
        assert ("%pax", "default") not in tint_calls

        conn = sqlite3.connect(app_env.db_path)
        actual_persona_id = conn.execute(
            "SELECT persona_id FROM instances WHERE id = ?", (iid,)
        ).fetchone()[0]
        conn.close()
        assert actual_persona_id == persona_id

    def test_set_legion_invalid(self, client):
        iid = _insert_instance()
        resp = client.patch(f"/api/instances/{iid}/legion", json={"legion": "unknown"})
        assert resp.status_code == 400

    def test_set_legion_not_found(self, client):
        resp = client.patch("/api/instances/nonexistent-id/legion", json={"legion": "custodes"})
        assert resp.status_code == 404


# ── 3. PATCH /api/instances/{id}/synced ──────────────────────


class TestSetSynced:
    # The /synced endpoint is a generic per-persona mode-conflict guard; it is
    # NOT how the Custodes singleton is identified anymore (that is persona + rank),
    # so conflict tests use a persona-backed Astartes legion rather than Custodes.
    def test_set_synced_true(self, client):
        iid = _insert_instance(legion="mechanicus")
        resp = client.patch(f"/api/instances/{iid}/synced", json={"synced": True})
        assert resp.status_code == 200
        assert resp.json()["synced"] is True
        assert _get_instance(iid)["synced"] == 1

    def test_set_synced_false(self, client):
        iid = _insert_instance(legion="mechanicus", synced=1)
        resp = client.patch(f"/api/instances/{iid}/synced", json={"synced": False})
        assert resp.status_code == 200
        assert resp.json()["synced"] is False
        assert _get_instance(iid)["synced"] == 0

    def test_synced_one_per_legion(self, client):
        """Second synced=true in same legion should 409."""
        iid1 = _insert_instance(legion="ultramarines")
        iid2 = _insert_instance(legion="ultramarines")

        resp1 = client.patch(f"/api/instances/{iid1}/synced", json={"synced": True})
        assert resp1.status_code == 200

        resp2 = client.patch(f"/api/instances/{iid2}/synced", json={"synced": True})
        assert resp2.status_code == 409

    def test_synced_different_legions(self, client):
        """Different legions can each have a synced session."""
        iid1 = _insert_instance(legion="astartes")
        iid2 = _insert_instance(legion="mechanicus")

        resp1 = client.patch(f"/api/instances/{iid1}/synced", json={"synced": True})
        assert resp1.status_code == 200

        resp2 = client.patch(f"/api/instances/{iid2}/synced", json={"synced": True})
        assert resp2.status_code == 200

    def test_synced_stopped_no_conflict(self, client):
        """Stopped instance with synced=1 shouldn't block new synced."""
        iid1 = _insert_instance(legion="ultramarines", synced=1, status="stopped")
        iid2 = _insert_instance(legion="ultramarines")

        resp = client.patch(f"/api/instances/{iid2}/synced", json={"synced": True})
        assert resp.status_code == 200

    def test_set_synced_not_found(self, client):
        resp = client.patch("/api/instances/nonexistent/synced", json={"synced": True})
        assert resp.status_code == 404


# ── 4. GET /api/legion/{legion}/synced-session ───────────────


def _insert_canonical_custodes(*, rank="overseer", status="working", db_path=None):
    """Insert a canonical instances row for the custodes persona (no sync mode).

    get_synced_session('custodes') resolves by persona + rank on the instances
    table now, so the synced-session lookup must find THIS row, not a
    claude_instances.synced flag.
    """
    iid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path or _TEST_DB_PATH)
    persona_id = conn.execute("SELECT id FROM personas WHERE slug='custodes'").fetchone()[0]
    conn.execute(
        """INSERT INTO instances
           (id, name, engine, working_dir, device_id, origin_type, commander_type,
            status, created_at, last_activity, persona_id, rank, automated,
            notification_mode, interaction_mode)
           VALUES (?, ?, 'claude', '/tmp', 'Mac-Mini', 'local', 'emperor',
                   ?, ?, ?, ?, ?, 0, 'verbose', 'text')""",
        (iid, f"Custodes-{iid[:6]}", status, now, now, persona_id, rank),
    )
    conn.commit()
    conn.close()
    return iid


class TestSyncedSessionLookup:
    def test_custodes_session_found_by_persona_rank(self, client):
        """Custodes resolves by persona + rank on instances — NO sync marker needed."""
        iid = _insert_canonical_custodes(rank="overseer", status="working")
        resp = client.get("/api/legion/custodes/synced-session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["synced_session"] is not None
        assert data["synced_session"]["id"] == iid
        assert data["synced_session"]["rank"] == "overseer"

    def test_custodes_session_none_when_no_live_custodes(self, client):
        resp = client.get("/api/legion/custodes/synced-session")
        assert resp.status_code == 200
        assert resp.json()["synced_session"] is None

    def test_custodes_session_retired_excluded(self, client):
        """A retired (superseded) custodes is not the live singleton."""
        _insert_canonical_custodes(rank="retired", status="stopped")
        resp = client.get("/api/legion/custodes/synced-session")
        assert resp.status_code == 200
        assert resp.json()["synced_session"] is None

    def test_mechanicus_session_uses_legacy_synced(self, client):
        """Non-custodes legions keep the legacy claude_instances.synced lookup."""
        iid = _insert_instance(legion="mechanicus", synced=1)
        resp = client.get("/api/legion/mechanicus/synced-session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["synced_session"] is not None
        assert data["synced_session"]["id"] == iid

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

    def test_civic_autodetect_pax_env_preserves_assigned_astartes_persona(self, client) -> None:
        sid = str(uuid.uuid4())
        self._register_via_hook(client, working_dir="/Volumes/Imperium/Pax-ENV", session_id=sid)
        row = _get_instance(sid)
        assert row is not None
        assert row["primarch"] is None
        with sqlite3.connect(_TEST_DB_PATH) as conn:
            persona_id = conn.execute(
                "SELECT persona_id FROM instances WHERE id = ?", (sid,)
            ).fetchone()[0]
        assert persona_id is not None

    def test_civic_pax_tint_keeps_assigned_chapter_not_default(self, client, monkeypatch) -> None:
        import shared

        tint_calls = []
        monkeypatch.setattr(
            shared,
            "apply_pane_tint",
            lambda pane, pane_tint, **kw: tint_calls.append((pane, pane_tint)),
        )

        sid = str(uuid.uuid4())
        resp = client.post(
            "/api/hooks/SessionStart",
            json={
                "session_id": sid,
                "cwd": "/Volumes/Imperium/Pax-ENV",
                "env": {},
                "pid": 99999,
                "tmux_pane": "%pax",
            },
        )
        assert resp.status_code == 200, resp.text
        assert tint_calls
        assert ("%pax", "default") not in tint_calls
        assert ("%pax", "#083010") not in tint_calls

    def test_civic_autodetect_pax_path_preserves_assigned_astartes_persona(self, client) -> None:
        sid = str(uuid.uuid4())
        self._register_via_hook(client, working_dir="/mnt/imperium/pax/project", session_id=sid)
        row = _get_instance(sid)
        assert row is not None
        assert row["primarch"] is None
        with sqlite3.connect(_TEST_DB_PATH) as conn:
            persona_id = conn.execute(
                "SELECT persona_id FROM instances WHERE id = ?", (sid,)
            ).fetchone()[0]
        assert persona_id is not None

    def test_no_autodetect_normal_dir(self, client):
        sid = str(uuid.uuid4())
        self._register_via_hook(
            client, working_dir="/Volumes/Imperium/Imperium-ENV", session_id=sid
        )
        row = _get_instance(sid)
        assert row is not None
        assert row["legion"] == "astartes"

    def test_normal_session_start_applies_chapter_persona_tint(self, client, monkeypatch):
        import shared

        tint_calls = []
        monkeypatch.setattr(
            shared,
            "apply_pane_tint",
            lambda pane, pane_tint, **kw: tint_calls.append((pane, pane_tint)),
        )

        sid = str(uuid.uuid4())
        resp = client.post(
            "/api/hooks/SessionStart",
            json={
                "session_id": sid,
                "cwd": "/Volumes/Imperium/Imperium-ENV",
                "env": {},
                "pid": 99999,
                "tmux_pane": "%chapter",
            },
        )
        assert resp.status_code == 200, resp.text
        assert ("%chapter", "#2a1020") in tint_calls

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


# ── 7b. Persona pane auto-setup (no self-PATCH) ──────────────


class TestPersonaPaneAutoSetup:
    """A fresh spawn in a persona/orchestrator pane is registered with the
    persona's canonical identity from the @PANE_ID label alone — no env legion/
    primarch/type and no manual PATCH. (Generalized from the custodes-only case.)"""

    def _register(self, client, pane_label, tmux_pane):
        sid = str(uuid.uuid4())
        resp = client.post(
            "/api/hooks/SessionStart",
            json={
                "session_id": sid,
                "cwd": "/Volumes/Imperium/Imperium-ENV",
                "pid": 99999,
                "pane_label": pane_label,
                "tmux_pane": tmux_pane,
                "env": {},
            },
        )
        assert resp.status_code == 200, f"Hook failed: {resp.text}"
        row = _get_instance(sid)
        assert row is not None
        return row

    def test_custodes_pane(self, client):
        row = self._register(client, "council:custodes", "%32")
        assert row["legion"] == "custodes"
        assert row["primarch"] == "custodes"
        # Custodes resting identity is persona + rank, NOT sync. The legacy
        # instance_type compatibility alias remains one_off until the morning
        # session sets sync MODE while live. synced default stays 0.
        assert row["instance_type"] == "one_off"
        assert row["synced"] == 0
        # Custodes is the one persona that speaks: reserved George voice.
        assert row["profile_name"] == "custodes"
        assert row["tts_voice"] == "Microsoft George"
        # The canonical identity that actually resolves the singleton: persona +
        # overseer rank on the instances table (stamped by the rank-stamp trigger).
        conn = sqlite3.connect(_TEST_DB_PATH)
        ident = conn.execute(
            """SELECT p.slug, i.rank FROM instances i
               JOIN personas p ON p.id = i.persona_id WHERE i.id = ?""",
            (row["id"],),
        ).fetchone()
        conn.close()
        assert ident is not None
        assert ident[0] == "custodes"
        assert ident[1] == "overseer"

    def test_fabricator_general_pane(self, client):
        # FG owns the dedicated singleton legion "fabricator" (not "mechanicus" —
        # that prefix is the tmux region). Matches assertions._row_matches_persona.
        row = self._register(client, "mechanicus:fabricator-general", "%40")
        assert row["legion"] == "fabricator"
        assert row["primarch"] == "fabricator-general"
        assert row["instance_type"] == "one_off"
        assert row["synced"] == 0
        # Voiceless persona: no TTS voice (frees a chapter voice slot for workers).
        assert row["profile_name"] == "fabricator-general"
        assert row["tts_voice"] is None

    def test_administratum_pane(self, client):
        # Administratum resolves on primarch='administratum' — the field that
        # MUST be backfilled or the recorder becomes unfindable.
        row = self._register(client, "council:administratum", "%41")
        assert row["legion"] == "mechanicus"
        assert row["primarch"] == "administratum"
        assert row["instance_type"] == "one_off"
        assert row["synced"] == 0
        # Voiceless persona: no TTS voice (frees a chapter voice slot for workers).
        assert row["profile_name"] == "administratum"
        assert row["tts_voice"] is None

    def test_worker_pane_defaults(self, client):
        """A non-persona pane (mechanicus worker) still defaults
        astartes/one_off/synced=0 with no primarch — pane identity is reserved
        for the singleton orchestrators."""
        row = self._register(client, "mechanicus:worker-1", "%77")
        assert row["legion"] == "astartes"
        assert row["instance_type"] == "one_off"
        assert row["synced"] == 0
        assert not row["primarch"]

    def test_pane_legion_is_authoritative(self, client):
        """The pane is authoritative for the persona's legion: even a conflicting
        env legion yields the pane's legion on the row (you can't mis-register a
        persona pane by passing the wrong legion)."""
        sid = str(uuid.uuid4())
        resp = client.post(
            "/api/hooks/SessionStart",
            json={
                "session_id": sid,
                "cwd": "/Volumes/Imperium/Imperium-ENV",
                "pid": 99999,
                "pane_label": "mechanicus:fabricator-general",
                "tmux_pane": "%42",
                "env": {"TOKEN_API_LEGION": "civic"},
            },
        )
        assert resp.status_code == 200, f"Hook failed: {resp.text}"
        row = _get_instance(sid)
        assert row["legion"] == "fabricator"


# ── 7d. Day-start custodes daily-note rebind ─────────────────


class TestCustodesDocRebind:
    def _insert_doc(self, db_path, file_path, title="doc"):
        conn = sqlite3.connect(db_path)
        now = datetime.now().isoformat()
        cur = conn.execute(
            """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
               VALUES (?, ?, ?, 'active', ?, ?)""",
            (title, file_path, None, now, now),
        )
        conn.commit()
        doc_id = cur.lastrowid
        conn.close()
        return doc_id

    def _insert_custodes(self, db_path, *, session_doc_id, status="idle"):
        iid = str(uuid.uuid4())
        conn = sqlite3.connect(db_path)
        now = datetime.now().isoformat()
        persona_id = conn.execute("SELECT id FROM personas WHERE slug = 'custodes'").fetchone()[0]
        commander_id = "test-custodes-commander"
        if not conn.execute("SELECT 1 FROM instances WHERE id = ?", (commander_id,)).fetchone():
            conn.execute(
                """INSERT INTO instances
                   (id, name, working_dir, origin_type, device_id, status, persona_id,
                    rank, commander_type, created_at, last_activity)
                   VALUES (?, 'Test Custodes Commander', '/tmp', 'local', 'Mac-Mini',
                           'idle', ?, 'overseer', 'emperor', ?, ?)""",
                (commander_id, persona_id, now, now),
            )
        conn.execute(
            """INSERT INTO instances
               (id, name, working_dir, origin_type, device_id, status, persona_id,
                rank, commander_type, commander_id, golden_throne, session_doc_id,
                created_at, last_activity)
               VALUES (?, ?, '/tmp', 'local', 'Mac-Mini', ?, ?, 'overseer',
                       'chapter', ?, 'sync', ?, ?, ?)""",
            (
                iid,
                f"Custodes-{iid[:6]}",
                status,
                persona_id,
                commander_id,
                session_doc_id,
                now,
                now,
            ),
        )
        conn.commit()
        conn.close()
        return iid

    def test_rebind_prior_day_leaves_bespoke(self, app_env, monkeypatch):
        import sys

        db_path = str(app_env.db_path)
        day_start = sys.modules["routes.day_start"]
        helpers = sys.modules.get("session_doc_helpers")
        if helpers is None:
            import session_doc_helpers as helpers

        base = "/x/Terra/Journal/Daily"
        today_id = self._insert_doc(db_path, f"{base}/2026-06-02.md", "2026-06-02")
        yday_id = self._insert_doc(db_path, f"{base}/2026-06-01.md", "2026-06-01")
        docket_id = self._insert_doc(db_path, "/x/Mars/Sessions/some-fix.md", "some-fix")

        stale = self._insert_custodes(db_path, session_doc_id=yday_id)
        bespoke = self._insert_custodes(db_path, session_doc_id=docket_id)
        current = self._insert_custodes(db_path, session_doc_id=today_id)
        stopped = self._insert_custodes(db_path, session_doc_id=yday_id, status="stopped")

        async def _fake_today(db, date_str=None):
            return today_id

        monkeypatch.setattr(helpers, "resolve_or_create_today_daily_note_session_doc", _fake_today)

        result = asyncio.run(day_start._consumer_custodes_doc_rebind())

        rebound_ids = {r["instance_id"] for r in result["rebound"]}
        assert stale in rebound_ids
        assert bespoke not in rebound_ids
        assert current not in rebound_ids
        assert stopped not in rebound_ids

        assert _get_instance(stale)["session_doc_id"] == today_id
        assert _get_instance(bespoke)["session_doc_id"] == docket_id
        assert _get_instance(current)["session_doc_id"] == today_id


# ── 7c. Daily-note tab/session-doc mismatch exemption ────────


class TestTabNameMismatchDailyNote:
    def test_date_slug_never_mismatches(self, app_env):
        """A persona tab vs a date-named daily note is not a real mismatch."""
        fn = app_env.main._tab_name_session_doc_mismatch
        assert fn("Custodes", "/x/Terra/Journal/Daily/2026-06-02.md") is False

    def test_real_drift_still_flagged(self, app_env):
        """A descriptive tab vs an unrelated descriptive slug is still a mismatch."""
        fn = app_env.main._tab_name_session_doc_mismatch
        assert fn("foo-fix", "/x/Mars/Sessions/bar-baz.md") is True


# ── 8. Workflow / continuity state ───────────────────────────


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
                    "TOKEN_API_LAUNCHER": "dispatch",
                    "TOKEN_API_DISPATCH_TARGET": "mechanicus:new",
                    "TOKEN_API_DISPATCH_WINDOW": "legion",
                    "TOKEN_API_DISPATCH_MODE": "stack_new",
                    "TOKEN_API_DISPATCH_SESSION_DOC_PATH": str(session_doc),
                    "TOKEN_API_TARGET_WORKING_DIR": "/Volumes/Imperium/runtimes/token-os/live",
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
                "cwd": "/Volumes/Imperium/runtimes/token-os/live",
                "pid": 22222,
                "env": {
                    "TOKEN_API_ENGINE": "codex",
                    "TOKEN_API_LAUNCHER": "dispatch",
                    "TOKEN_API_DISPATCH_TARGET": "bridge:SW",
                    "TOKEN_API_DISPATCH_WINDOW": "bridge",
                    "TOKEN_API_DISPATCH_MODE": "named_slot",
                    "TOKEN_API_DISPATCH_SLOT": "SW",
                    "TOKEN_API_DISPATCH_SESSION_DOC_PATH": str(session_doc),
                    "TOKEN_API_TARGET_WORKING_DIR": "/Volumes/Imperium/runtimes/token-os/live",
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
            """UPDATE legacy_instances
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


# ── PATCH /api/instances/{id}/tmux-pane ──────────────────────


class TestSetTmuxPane:
    """Pane rebinding is retired. Tmux runtime identity belongs to tmuxctl."""

    def test_rebind_tmux_pane_route_removed(self, client: TestClient) -> None:
        iid = _insert_instance(status="processing")
        resp = client.patch(f"/api/instances/{iid}/tmux-pane", json={"tmux_pane": "%16"})
        assert resp.status_code == 404

    def test_rebind_tmux_pane_nonexistent_route_removed(self, client: TestClient) -> None:
        resp = client.patch("/api/instances/nonexistent-id/tmux-pane", json={"tmux_pane": "%16"})
        assert resp.status_code == 404
