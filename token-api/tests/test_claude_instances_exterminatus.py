"""Exterminatus tests: `claude_instances` is extracted to archive.db, `instances` is sole truth.

Written RED-first for the claude-instances-archive-extraction branch:
- fresh DBs never create `claude_instances`
- existing DBs get a one-shot, idempotent, reversible extraction into
  `<db dir>/archive/archive.db` (row counts verified), then the live table drops
- `instances` rows are authoritative on merge; legacy rows only backfill
  runtime-annex fields; legacy-only rows stay in the archive
- `instance_mutations`/`workflow_events` are rebuilt without the FK to
  `claude_instances` so provenance logging survives the drop
- pane_state_queue triggers live on `instances` and push instance status vocab
- sanctioned writes touch only `instances`
- legacy PATCH endpoints (/legion, /synced, /type) write instance-table semantics
"""

import sqlite3
import uuid

import pytest


def _db(app_env):
    conn = sqlite3.connect(app_env.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _archive_path(app_env):
    return app_env.db_path.parent / "archive" / "archive.db"


def _table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


@pytest.fixture
def client(app_env):
    from fastapi.testclient import TestClient

    return TestClient(app_env.main.app)


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


# ── legacy-DB seed (simulates a live pre-extraction agents.db) ──────────────

LEGACY_ACTIVE_ID = "11111111-1111-1111-1111-111111111111"
LEGACY_ONLY_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def legacy_seed(tmp_path):
    """Build a pre-extraction agents.db BEFORE app_env's init runs.

    Mirrors the live upgrade shape: a populated claude_instances, an instances
    table (old 22-column layout) whose identity fields diverge from
    the legacy projection, and an instance_mutations table carrying the
    legacy FK.
    """
    db_path = tmp_path / "agents.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE claude_instances (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            tab_name TEXT,
            working_dir TEXT,
            origin_type TEXT NOT NULL DEFAULT 'local',
            device_id TEXT NOT NULL DEFAULT 'test-device',
            status TEXT DEFAULT 'idle',
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            stopped_at TIMESTAMP,
            legion TEXT DEFAULT 'astartes',
            synced INTEGER DEFAULT 0,
            instance_type TEXT DEFAULT 'one_off',
            tmux_pane TEXT,
            pane_label TEXT,
            workflow_state TEXT,
            engine TEXT,
            is_subagent INTEGER DEFAULT 0,
            hook_driven INTEGER DEFAULT 0,
            zealotry INTEGER DEFAULT 4,
            session_doc_id INTEGER
        );
        CREATE TABLE instances (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            engine TEXT,
            working_dir TEXT,
            device_id TEXT NOT NULL,
            origin_type TEXT NOT NULL DEFAULT 'local',
            commander_type TEXT NOT NULL DEFAULT 'emperor',
            commander_id TEXT,
            status TEXT NOT NULL DEFAULT 'idle',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_activity TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            stopped_at TIMESTAMP,
            archived_at TIMESTAMP,
            persona_id TEXT,
            rank TEXT NOT NULL DEFAULT 'astartes',
            session_doc_id INTEGER,
            continuity_binding_source TEXT,
            wrapper_launch_id TEXT,
            automated INTEGER NOT NULL DEFAULT 0,
            notification_mode TEXT NOT NULL DEFAULT 'verbose',
            interaction_mode TEXT NOT NULL DEFAULT 'text',
            golden_throne TEXT
        );
        CREATE TABLE instance_mutations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL,
            mutation_type TEXT NOT NULL,
            write_source TEXT NOT NULL,
            write_txn_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            service_version TEXT,
            wrapper_launch_id TEXT,
            field_names_json TEXT,
            before_json TEXT,
            after_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (instance_id) REFERENCES claude_instances(id)
        );
        CREATE TABLE workflow_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL,
            workflow_state TEXT,
            event_type TEXT NOT NULL,
            event_owner TEXT,
            details_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (instance_id) REFERENCES claude_instances(id)
        );
        """
    )
    # Active session: present in BOTH tables. The legacy row carries runtime
    # annex data; the instance row carries identity the old projection used to
    # clobber (rank=primarch, working vocab).
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, status, legion, synced,
            instance_type, tmux_pane, workflow_state, engine)
           VALUES (?, ?, 'fg-session', '/tmp/fg', 'processing', 'custodes', 1,
                   'sync', '%42', 'open', 'claude')""",
        (LEGACY_ACTIVE_ID, LEGACY_ACTIVE_ID),
    )
    conn.execute(
        """INSERT INTO instances
           (id, name, engine, working_dir, device_id, status, rank,
            commander_type, origin_type)
           VALUES (?, 'fg-session', 'claude', '/tmp/fg', 'test-device',
                   'working', 'primarch', 'emperor', 'perpetual')""",
        (LEGACY_ACTIVE_ID,),
    )
    # Ancient stopped row that exists ONLY in claude_instances: must be
    # archived, must NOT enter live instances.
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, status, stopped_at)
           VALUES (?, ?, 'ancient', 'stopped', '2026-01-01T00:00:00')""",
        (LEGACY_ONLY_ID, LEGACY_ONLY_ID),
    )
    # Historical mutation row referencing the archived-only instance.
    conn.execute(
        """INSERT INTO instance_mutations
           (instance_id, mutation_type, write_source, write_txn_id, actor)
           VALUES (?, 'instance_updated', 'test', 'txn-legacy', 'seed')""",
        (LEGACY_ONLY_ID,),
    )
    conn.commit()
    conn.close()
    return db_path


# ── fresh DB ─────────────────────────────────────────────────────────────────


class TestFreshDatabase:
    def test_fresh_init_creates_no_claude_instances(self, app_env):
        conn = _db(app_env)
        names = _table_names(conn)
        conn.close()
        assert "claude_instances" not in names
        assert "instances" in names

    def test_instances_has_runtime_annex_columns(self, app_env):
        conn = _db(app_env)
        cols = _columns(conn, "instances")
        conn.close()
        for annex in (
            "workflow_state",
            "planning_state",
            "tts_voice",
            "notification_sound",
            "discord_channel",
            "input_lock",
            "pr_state",
            "victory_at",
            "is_subagent",
            "hook_driven",
            "zealotry",
            "dispatch_target",
        ):
            assert annex in cols, f"missing runtime annex column: {annex}"
        # the dead identity duplicates must NOT be reincarnated — and pane ids
        # (tmux_pane/pane_label) are EXTERMINATED: pane geometry lives only in the
        # tmuxctl @INSTANCE_ID oracle, never persisted.
        for dead in (
            "legion",
            "primarch",
            "profile_name",
            "instance_type",
            "synced",
            "tab_name",
            "tmux_pane",
            "pane_label",
        ):
            assert dead not in cols, f"dead legacy column reincarnated on instances: {dead}"

    def test_mutation_tables_have_no_legacy_fk(self, app_env):
        conn = _db(app_env)
        for table in ("instance_mutations", "workflow_events"):
            fks = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            referenced = {fk[2] for fk in fks}
            assert "claude_instances" not in referenced, f"{table} still references the corpse"
        conn.close()

    def test_session_start_registers_into_instances_only(self, client, app_env):
        instance_id = _session_start(client)
        conn = _db(app_env)
        row = conn.execute(
            "SELECT id, name, working_dir, status FROM instances WHERE id = ?",
            (instance_id,),
        ).fetchone()
        names = _table_names(conn)
        conn.close()
        assert row is not None, "SessionStart did not register into instances"
        assert "claude_instances" not in names

    def test_status_trigger_pushes_instance_vocab_to_pane_state_queue(self, client, app_env):
        instance_id = _session_start(client, tmux_pane="%99")
        conn = _db(app_env)
        conn.execute(
            "UPDATE instances SET status = 'working' WHERE id = ?",
            (instance_id,),
        )
        conn.commit()
        row = conn.execute(
            """SELECT variable, value FROM pane_state_queue
               WHERE instance_id = ? ORDER BY id DESC LIMIT 1""",
            (instance_id,),
        ).fetchone()
        conn.close()
        assert row is not None, "status trigger on instances did not fire"
        assert row["variable"] == "@CC_STATE"
        assert row["value"] == "working"


# ── upgrade extraction ───────────────────────────────────────────────────────


class TestArchiveExtraction:
    def test_legacy_rows_extracted_to_archive_db(self, legacy_seed, app_env):
        archive = _archive_path(app_env)
        assert archive.exists(), "archive.db was not created"

        aconn = sqlite3.connect(archive)
        count = aconn.execute("SELECT count(*) FROM claude_instances").fetchone()[0]
        ids = {row[0] for row in aconn.execute("SELECT id FROM claude_instances").fetchall()}
        aconn.close()
        assert count == 2, "archive row count != legacy row count"
        assert ids == {LEGACY_ACTIVE_ID, LEGACY_ONLY_ID}

        conn = _db(app_env)
        names = _table_names(conn)
        conn.close()
        assert "claude_instances" not in names, "legacy table still in the live DB"

    def test_instance_identity_wins_and_annex_backfills(self, legacy_seed, app_env):
        conn = _db(app_env)
        row = conn.execute(
            """SELECT rank, origin_type, status, workflow_state, golden_throne
               FROM instances WHERE id = ?""",
            (LEGACY_ACTIVE_ID,),
        ).fetchone()
        conn.close()
        assert row is not None
        # singleton rank reconciliation applies, while legacy defaults do not re-project origin/status
        assert row["rank"] == "overseer"
        assert row["origin_type"] == "perpetual"
        assert row["status"] == "working"
        # annex backfilled from the legacy row (pane ids are NOT persisted — exterminated)
        assert row["workflow_state"] == "open"
        # synced=1/instance_type=sync legacy markers land as the instance marker
        assert row["golden_throne"] == "sync"

    def test_legacy_only_rows_stay_in_archive(self, legacy_seed, app_env):
        conn = _db(app_env)
        row = conn.execute("SELECT id FROM instances WHERE id = ?", (LEGACY_ONLY_ID,)).fetchone()
        conn.close()
        assert row is None, "ancient legacy-only row leaked into live instances"

    def test_extraction_is_idempotent(self, legacy_seed, app_env):
        # Second init over the already-extracted DB must not error or
        # duplicate archive rows.
        app_env.init_db.init_database()
        aconn = sqlite3.connect(_archive_path(app_env))
        count = aconn.execute("SELECT count(*) FROM claude_instances").fetchone()[0]
        aconn.close()
        assert count == 2

    def test_mutation_logging_survives_the_drop(self, legacy_seed, app_env):
        conn = _db(app_env)
        conn.execute("PRAGMA foreign_keys=ON")
        # historical rows preserved through the FK-free rebuild
        legacy_rows = conn.execute(
            "SELECT count(*) FROM instance_mutations WHERE write_txn_id = 'txn-legacy'"
        ).fetchone()[0]
        assert legacy_rows == 1
        # new provenance writes must not hit a dangling FK
        conn.execute(
            """INSERT INTO instance_mutations
               (instance_id, mutation_type, write_source, write_txn_id, actor)
               VALUES (?, 'instance_updated', 'test', 'txn-new', 'test')""",
            (LEGACY_ACTIVE_ID,),
        )
        conn.execute(
            """INSERT INTO workflow_events (instance_id, event_type)
               VALUES (?, 'test_event')""",
            (LEGACY_ACTIVE_ID,),
        )
        conn.commit()
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.close()
        assert violations == []

    def test_restore_is_possible(self, legacy_seed, app_env):
        """Reversibility: the documented restore path brings the table back."""
        from db_schema import restore_claude_instances_from_archive

        restore_claude_instances_from_archive(app_env.db_path)
        conn = _db(app_env)
        count = conn.execute("SELECT count(*) FROM claude_instances").fetchone()[0]
        conn.close()
        assert count == 2


# ── sanctioned writes ────────────────────────────────────────────────────────


class TestSanctionedWritesV2Only:
    def test_sanctioned_insert_writes_instances(self, app_env):
        import instance_mutation

        conn = _db(app_env)
        instance_id = str(uuid.uuid4())
        instance_mutation.sanctioned_insert_instance_sync(
            conn,
            values={
                "id": instance_id,
                "name": "exterminatus-test",
                "device_id": "test-device",
                "status": "idle",
            },
            mutation_type="instance_registered",
            write_source="test",
            actor="test",
        )
        conn.commit()
        row = conn.execute("SELECT name FROM instances WHERE id = ?", (instance_id,)).fetchone()
        conn.close()
        assert row is not None and row["name"] == "needs-name"


# ── legacy PATCH endpoints write instance-table ─────────────────────────────────────────


class TestLegacyPatchEndpoints:
    def test_patch_legion_sets_persona(self, client, app_env):
        instance_id = _session_start(client)
        resp = client.patch(f"/api/instances/{instance_id}/legion", json={"legion": "custodes"})
        assert resp.status_code == 200, resp.text
        conn = _db(app_env)
        row = conn.execute(
            """SELECT p.slug FROM instances i JOIN personas p ON p.id = i.persona_id
               WHERE i.id = ?""",
            (instance_id,),
        ).fetchone()
        conn.close()
        assert row is not None and row["slug"] == "custodes"

    def test_patch_synced_sets_golden_throne_marker(self, client, app_env):
        instance_id = _session_start(client)
        resp = client.patch(f"/api/instances/{instance_id}/synced", json={"synced": True})
        assert resp.status_code == 200, resp.text
        conn = _db(app_env)
        row = conn.execute(
            "SELECT golden_throne FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        assert row["golden_throne"] == "sync"

        resp = client.patch(f"/api/instances/{instance_id}/synced", json={"synced": False})
        assert resp.status_code == 200, resp.text
        row = conn.execute(
            "SELECT golden_throne FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        conn.close()
        assert row["golden_throne"] is None

    def test_patch_type_golden_throne_creates_gt_row(self, client, app_env):
        instance_id = _session_start(client)
        resp = client.patch(
            f"/api/instances/{instance_id}/type",
            json={"instance_type": "golden_throne", "zealotry": 6},
        )
        assert resp.status_code == 200, resp.text
        conn = _db(app_env)
        row = conn.execute(
            "SELECT golden_throne, zealotry FROM instances WHERE id = ?",
            (instance_id,),
        ).fetchone()
        assert row["golden_throne"] not in (None, "sync")
        assert row["zealotry"] == 6
        gt = conn.execute(
            "SELECT id FROM golden_throne WHERE CAST(id AS TEXT) = ?",
            (row["golden_throne"],),
        ).fetchone()
        conn.close()
        assert gt is not None, "golden_throne marker does not reference a golden_throne row"

    def test_patch_type_one_off_clears_marker(self, client, app_env):
        instance_id = _session_start(client)
        client.patch(f"/api/instances/{instance_id}/synced", json={"synced": True})
        resp = client.patch(f"/api/instances/{instance_id}/type", json={"instance_type": "one_off"})
        assert resp.status_code == 200, resp.text
        conn = _db(app_env)
        row = conn.execute(
            "SELECT golden_throne FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        conn.close()
        assert row["golden_throne"] is None
