"""
SQLite schema and migrations for Token-API.

All bootstrap paths should go through this module:
- FastAPI startup
- standalone init_db.py
- tests
"""

import asyncio
import os
import sqlite3
from pathlib import Path

import aiosqlite

from cron_engine import CronEngine
from instance_registry import (
    INSTANCE_COLUMNS,
    RUNTIME_ANNEX_COLUMNS,
    golden_throne_binding,
    legacy_row_to_instance_values,
    slug_from_legacy,
)
from personas import (
    active_non_retired_persona_ids,
    assign_astartes_persona,
    ensure_personas_table,
    persona_id_for_slug,
    persona_to_profile,
    resolve_persona,
    singleton_persona_slug_for_runtime,
)

DEFAULT_DB_PATH = Path(os.environ.get("TOKEN_API_DB", Path.home() / ".claude" / "agents.db"))


def archive_db_path_for(db_path: Path) -> Path:
    """Archive DB for extracted legacy tables: <db dir>/archive/archive.db.

    Override with TOKEN_API_ARCHIVE_DB (tests, ad-hoc restores).
    """
    env = os.environ.get("TOKEN_API_ARCHIVE_DB")
    if env:
        return Path(env)
    return db_path.parent / "archive" / "archive.db"


async def _table_columns(db, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {col[1] for col in await cursor.fetchall()}


async def _table_exists(db, table: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    )
    return await cursor.fetchone() is not None


async def _persona_id_for_legacy_row(db, row: dict) -> str | None:
    slug = slug_from_legacy(row)
    if not slug:
        return None
    cursor = await db.execute("SELECT id FROM personas WHERE slug = ?", (slug,))
    found = await cursor.fetchone()
    if found:
        return found[0]
    persona_id = persona_id_for_slug(slug)
    display = slug.replace("-", " ").title()
    await db.execute(
        """INSERT INTO personas
           (id, slug, display_name, default_rank, pane_tint)
           VALUES (?, ?, ?, 'astartes', 'default')
           ON CONFLICT(id) DO NOTHING""",
        (persona_id, slug, display),
    )
    return persona_id


async def _create_instances_table(db) -> None:
    await db.execute("""
        CREATE TABLE instances (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            engine TEXT,
            working_dir TEXT,
            device_id TEXT NOT NULL,
            origin_type TEXT NOT NULL DEFAULT 'local'
                CHECK(origin_type IN ('local','ssh','cron','dispatch','api','perpetual')),
            commander_type TEXT NOT NULL DEFAULT 'emperor'
                CHECK(commander_type IN ('emperor','persona','chapter')),
            commander_id TEXT,
            status TEXT NOT NULL DEFAULT 'idle'
                CHECK(status IN ('idle','working','questioning','preplanning','planning','compacting','reviewing','victorious','stopped','archived')),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_activity TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            stopped_at TIMESTAMP,
            archived_at TIMESTAMP,
            persona_id TEXT REFERENCES personas(id),
            rank TEXT NOT NULL DEFAULT 'astartes'
                CHECK(rank IN ('astartes','overseer','primarch','retired') OR rank GLOB 'aspirant:*'),
            session_doc_id INTEGER,
            continuity_binding_source TEXT,
            wrapper_launch_id TEXT,
            automated INTEGER NOT NULL DEFAULT 0 CHECK(automated IN (0,1)),
            notification_mode TEXT NOT NULL DEFAULT 'verbose'
                CHECK(notification_mode IN ('verbose','muted','silent')),
            interaction_mode TEXT NOT NULL DEFAULT 'text'
                CHECK(interaction_mode IN ('text','voice_chat')),
            golden_throne TEXT,
            -- ── RUNTIME ANNEX (transitional) ─────────────────────────────
            -- Inherited verbatim from the extracted claude_instances table so
            -- exterminatus could land without redesigning every subsystem.
            -- Keep in lockstep with instance_registry.RUNTIME_ANNEX_COLUMNS.
            -- Do NOT add columns here: each is slated for per-column removal
            -- (tmux geometry -> @INSTANCE_ID stamps, GT state -> golden_throne
            -- table, workflow/planning -> status enum).
            tmux_pane TEXT,
            pane_label TEXT,
            dispatch_target TEXT,
            dispatch_window TEXT,
            dispatch_mode TEXT,
            dispatch_slot TEXT,
            dispatch_session_doc_path TEXT,
            target_working_dir TEXT,
            launch_mode TEXT,
            launcher TEXT,
            transplant_target_session TEXT,
            transplant_expected INTEGER DEFAULT 0,
            input_lock TEXT,
            tts_voice TEXT,
            notification_sound TEXT,
            discord_hosted INTEGER DEFAULT 0,
            discord_channel TEXT,
            discord_bot TEXT,
            workflow_state TEXT,
            workflow_updated_at TIMESTAMP,
            workflow_blocked_reason TEXT,
            next_required_action TEXT,
            next_action_owner TEXT,
            planning_state TEXT DEFAULT 'none',
            planning_updated_at TIMESTAMP,
            planning_source TEXT,
            closure_surface TEXT,
            closure_required INTEGER DEFAULT 0,
            session_doc_policy TEXT,
            pr_url TEXT,
            pr_state TEXT,
            victory_at TIMESTAMP,
            victory_reason TEXT,
            is_subagent INTEGER DEFAULT 0,
            hook_driven INTEGER DEFAULT 0,
            zealotry INTEGER DEFAULT 4,
            gt_resume_count INTEGER DEFAULT 0,
            gt_resume_window_started_at TIMESTAMP,
            gt_last_resume_at TIMESTAMP,
            follow_up_sop TEXT,
            stop_allowed INTEGER DEFAULT 1,
            -- ── end runtime annex ────────────────────────────────────────
            CHECK((commander_type = 'emperor' AND commander_id IS NULL) OR
                  (commander_type IN ('persona','chapter') AND commander_id IS NOT NULL)),
            CHECK(status != 'archived' OR rank = 'retired')
        )
    """)


async def _repair_legacy_instance_personas(db: aiosqlite.Connection) -> int:
    """Last cutover repair for legacy active persona assignments before extraction."""
    cols = await _table_columns(db, "claude_instances")
    required = {"id", "profile_name", "tts_voice", "notification_sound", "status"}
    if not required.issubset(cols):
        return 0

    select_cols = [
        "id",
        "profile_name",
        "tts_voice",
        "notification_sound",
        "status",
    ]
    for optional in ("legion", "primarch", "is_subagent", "registered_at", "last_activity"):
        if optional in cols:
            select_cols.append(optional)

    subagent_clause = "AND COALESCE(is_subagent, 0) = 0" if "is_subagent" in cols else ""
    order_terms = [col for col in ("registered_at", "last_activity", "id") if col in cols]
    order_sql = ", ".join(order_terms or ["id"])
    cursor = await db.execute(
        f"""
        SELECT {", ".join(select_cols)}
        FROM claude_instances
        WHERE status IN ('processing', 'idle')
          {subagent_clause}
        ORDER BY {order_sql}
        """
    )
    rows = [dict(zip(select_cols, row, strict=False)) for row in await cursor.fetchall()]

    changed = 0
    locked_ids = await active_non_retired_persona_ids(db)
    for row in rows:
        singleton_slug = singleton_persona_slug_for_runtime(
            legion=row.get("legion"), primarch=row.get("primarch")
        )
        if singleton_slug:
            persona = await resolve_persona(db, singleton_slug)
            if not persona:
                continue
            profile = persona_to_profile(persona)
            updates = {
                "profile_name": profile["name"],
                "tts_voice": profile["wsl_voice"],
                "notification_sound": profile["notification_sound"],
            }
            if any(row.get(key) != value for key, value in updates.items()):
                await db.execute(
                    """
                    UPDATE claude_instances
                    SET profile_name = ?, tts_voice = ?, notification_sound = ?
                    WHERE id = ?
                    """,
                    (
                        updates["profile_name"],
                        updates["tts_voice"],
                        updates["notification_sound"],
                        row["id"],
                    ),
                )
                changed += 1
            locked_ids = await active_non_retired_persona_ids(db)
            continue

        current = await resolve_persona(db, row.get("profile_name") or "")
        if current and current.get("default_rank") == "astartes":
            continue

        assigned, _ = await assign_astartes_persona(db, active_ids=locked_ids)
        profile = persona_to_profile(assigned)
        await db.execute(
            """
            UPDATE claude_instances
            SET profile_name = ?, tts_voice = ?, notification_sound = ?
            WHERE id = ?
            """,
            (
                profile["name"],
                profile["wsl_voice"],
                profile["notification_sound"],
                row["id"],
            ),
        )
        locked_ids.add(assigned["id"])
        changed += 1

    return changed


async def _ensure_instances(db) -> None:
    await db.execute("PRAGMA foreign_keys=ON")
    await ensure_personas_table(db)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS golden_throne (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zealotry INTEGER NOT NULL DEFAULT 4,
            resume_count INTEGER NOT NULL DEFAULT 0,
            resume_window_started_at TIMESTAMP,
            last_resume_at TIMESTAMP,
            follow_up_sop TEXT,
            stop_allowed INTEGER NOT NULL DEFAULT 1 CHECK(stop_allowed IN (0,1)),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS aspirants (
            id TEXT PRIMARY KEY,
            source_note_path TEXT,
            prompt_path TEXT,
            system_prompt_path TEXT,
            status TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            promoted_instance_id TEXT,
            retired_instance_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
    """)

    needs_rebuild = True
    old_rows: list[dict] = []
    if await _table_exists(db, "instances"):
        cols = await _table_columns(db, "instances")
        needs_rebuild = cols != set(INSTANCE_COLUMNS)
        if needs_rebuild:
            db.row_factory = aiosqlite.Row
            try:
                cursor = await db.execute("SELECT * FROM instances")
                old_rows = [dict(row) for row in await cursor.fetchall()]
            finally:
                db.row_factory = None
            await db.execute("DROP TABLE instances")
    if needs_rebuild:
        await _create_instances_table(db)
        # Existing instance rows are the ONLY rebuild source. The legacy
        # claude_instances projection is gone: it used to take priority here,
        # clobbering instance identity with table defaults (rank/commander/origin)
        # and resurrecting ghost rows. Legacy data is extracted to archive.db
        # by _extract_claude_instances() instead.
        source_rows = old_rows
        for row in source_rows:
            if set(INSTANCE_COLUMNS).issubset(row.keys()):
                values = {column: row.get(column) for column in INSTANCE_COLUMNS}
            else:
                values = legacy_row_to_instance_values(
                    row, await _persona_id_for_legacy_row(db, row)
                )
            if not values.get("id"):
                continue
            columns = [column for column in INSTANCE_COLUMNS if column in values]
            await db.execute(
                f"INSERT OR REPLACE INTO instances ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [values[column] for column in columns],
            )

    columns = await _table_columns(db, "instances")
    if columns != set(INSTANCE_COLUMNS):
        raise RuntimeError(
            f"instances table schema mismatch: expected {INSTANCE_COLUMNS}, got {sorted(columns)}"
        )

    await db.execute("CREATE INDEX IF NOT EXISTS idx_instances_status ON instances(status)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_instances_device ON instances(device_id)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_instances_persona_active ON instances(persona_id, rank)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_instances_commander ON instances(commander_type, commander_id)"
    )
    await db.execute("DROP TRIGGER IF EXISTS trg_instances_persona_fk_guard")
    await db.execute("""
        CREATE TRIGGER trg_instances_persona_fk_guard
        BEFORE INSERT ON instances
        WHEN NEW.persona_id IS NOT NULL
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM personas WHERE id = NEW.persona_id
            ) THEN RAISE(ABORT, 'persona_id must reference personas.id') END;
        END
    """)
    await db.execute("DROP TRIGGER IF EXISTS trg_instances_persona_fk_guard_update")
    await db.execute("""
        CREATE TRIGGER trg_instances_persona_fk_guard_update
        BEFORE UPDATE OF persona_id ON instances
        WHEN NEW.persona_id IS NOT NULL
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM personas WHERE id = NEW.persona_id
            ) THEN RAISE(ABORT, 'persona_id must reference personas.id') END;
        END
    """)
    await db.execute("DROP TRIGGER IF EXISTS trg_instances_persona_commander_guard")
    await db.execute("""
        CREATE TRIGGER trg_instances_persona_commander_guard
        BEFORE INSERT ON instances
        WHEN NEW.commander_type = 'persona'
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM personas WHERE id = NEW.commander_id
            ) THEN RAISE(ABORT, 'persona commander_id must reference personas.id') END;
        END
    """)
    await db.execute("DROP TRIGGER IF EXISTS trg_instances_persona_commander_guard_update")
    await db.execute("""
        CREATE TRIGGER trg_instances_persona_commander_guard_update
        BEFORE UPDATE OF commander_type, commander_id ON instances
        WHEN NEW.commander_type = 'persona'
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM personas WHERE id = NEW.commander_id
            ) THEN RAISE(ABORT, 'persona commander_id must reference personas.id') END;
        END
    """)
    await db.execute("DROP TRIGGER IF EXISTS trg_instances_golden_throne_guard")
    await db.execute("""
        CREATE TRIGGER trg_instances_golden_throne_guard
        BEFORE INSERT ON instances
        WHEN NEW.golden_throne IS NOT NULL AND NEW.golden_throne != 'sync'
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM golden_throne WHERE CAST(id AS TEXT) = NEW.golden_throne
            ) THEN RAISE(ABORT, 'golden_throne must be NULL, sync, or golden_throne.id') END;
        END
    """)
    await db.execute("DROP TRIGGER IF EXISTS trg_instances_golden_throne_guard_update")
    await db.execute("""
        CREATE TRIGGER trg_instances_golden_throne_guard_update
        BEFORE UPDATE OF golden_throne ON instances
        WHEN NEW.golden_throne IS NOT NULL AND NEW.golden_throne != 'sync'
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM golden_throne WHERE CAST(id AS TEXT) = NEW.golden_throne
            ) THEN RAISE(ABORT, 'golden_throne must be NULL, sync, or golden_throne.id') END;
        END
    """)
    await db.execute("DROP TRIGGER IF EXISTS trg_instances_chapter_persona_guard")
    await db.execute("""
        CREATE TRIGGER trg_instances_chapter_persona_guard
        BEFORE INSERT ON instances
        WHEN NEW.commander_type = 'chapter'
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM instances commander
                WHERE commander.id = NEW.commander_id
                  AND commander.rank != 'retired'
                  AND commander.status != 'archived'
                  AND (commander.persona_id IS NEW.persona_id)
            ) THEN RAISE(ABORT, 'chapter commander must be active and share persona_id') END;
        END
    """)
    await db.execute("DROP TRIGGER IF EXISTS trg_instances_chapter_persona_guard_update")
    await db.execute("""
        CREATE TRIGGER trg_instances_chapter_persona_guard_update
        BEFORE UPDATE OF commander_type, commander_id, persona_id ON instances
        WHEN NEW.commander_type = 'chapter'
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM instances commander
                WHERE commander.id = NEW.commander_id
                  AND commander.rank != 'retired'
                  AND commander.status != 'archived'
                  AND (commander.persona_id IS NEW.persona_id)
            ) THEN RAISE(ABORT, 'chapter commander must be active and share persona_id') END;
        END
    """)
    await db.execute("DROP TRIGGER IF EXISTS trg_instances_retire_children")
    await db.execute("""
        CREATE TRIGGER trg_instances_retire_children
        AFTER UPDATE OF rank ON instances
        WHEN NEW.rank = 'retired' AND OLD.rank != 'retired'
        BEGIN
            UPDATE instances
               SET rank = 'retired', status = CASE WHEN status = 'archived' THEN 'archived' ELSE 'stopped' END, stopped_at = COALESCE(stopped_at, CURRENT_TIMESTAMP)
             WHERE commander_type = 'chapter' AND commander_id = NEW.id AND rank != 'retired';
        END
    """)
    await db.execute("DROP TRIGGER IF EXISTS trg_instances_singleton_guard")
    await db.execute("""
        CREATE TRIGGER trg_instances_singleton_guard
        BEFORE INSERT ON instances
        WHEN NEW.persona_id IS NOT NULL AND NEW.rank != 'retired' AND NEW.commander_type != 'chapter'
             AND COALESCE((SELECT default_rank FROM personas WHERE id = NEW.persona_id), 'astartes') != 'astartes'
        BEGIN
            UPDATE instances
               SET rank = 'retired', status = CASE WHEN status = 'archived' THEN 'archived' ELSE 'stopped' END, stopped_at = COALESCE(stopped_at, CURRENT_TIMESTAMP)
             WHERE persona_id = NEW.persona_id AND rank != 'retired' AND commander_type != 'chapter';
        END
    """)
    await db.execute("DROP TRIGGER IF EXISTS trg_instances_singleton_guard_update")
    await db.execute("""
        CREATE TRIGGER trg_instances_singleton_guard_update
        BEFORE UPDATE OF persona_id, rank, commander_type ON instances
        WHEN NEW.persona_id IS NOT NULL AND NEW.rank != 'retired' AND NEW.commander_type != 'chapter'
             AND COALESCE((SELECT default_rank FROM personas WHERE id = NEW.persona_id), 'astartes') != 'astartes'
        BEGIN
            UPDATE instances
               SET rank = 'retired', status = CASE WHEN status = 'archived' THEN 'archived' ELSE 'stopped' END, stopped_at = COALESCE(stopped_at, CURRENT_TIMESTAMP)
             WHERE id != NEW.id AND persona_id = NEW.persona_id AND rank != 'retired' AND commander_type != 'chapter';
        END
    """)


# ── claude_instances exterminatus ────────────────────────────────────────────
# One-shot, idempotent, reversible extraction of the legacy table into
# archive.db. After this runs, `instances` is the sole live instance
# table; the legacy data survives ONLY in the archive.

_LEGACY_FK_REBUILDS = {
    # replacement FK-free DDL for tables whose historical CREATE carried
    # `REFERENCES claude_instances(id)` — left in place, those FKs would
    # poison every insert after the drop (PRAGMA foreign_keys=ON).
    "workflow_events": """
        CREATE TABLE workflow_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL,
            workflow_state TEXT,
            event_type TEXT NOT NULL,
            event_owner TEXT,
            details_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "instance_mutations": """
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
}


async def _ordered_columns(db, table: str, schema: str = "main") -> list[str]:
    cursor = await db.execute(f"PRAGMA {schema}.table_info({table})")
    return [col[1] for col in await cursor.fetchall()]


async def _copy_legacy_table_to_archive(db, archive_path: Path) -> None:
    """Copy claude_instances (schema + all rows) into archive.db, verifying counts."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'claude_instances'"
    )
    ddl = (await cursor.fetchone())[0]

    # ATTACH cannot run inside a transaction; init has open DML by now.
    await db.commit()
    await db.execute("ATTACH DATABASE ? AS archive", (str(archive_path),))
    try:
        cursor = await db.execute(
            "SELECT 1 FROM archive.sqlite_master WHERE type = 'table' AND name = 'claude_instances'"
        )
        if await cursor.fetchone() is None:
            archive_ddl = ddl.replace("claude_instances", "archive.claude_instances", 1)
            if archive_ddl == ddl:
                raise RuntimeError("could not derive archive DDL for claude_instances")
            await db.execute(archive_ddl)

        live_cols = await _ordered_columns(db, "claude_instances")
        archive_cols = set(await _ordered_columns(db, "claude_instances", schema="archive"))
        copy_cols = ", ".join(col for col in live_cols if col in archive_cols)
        await db.execute(
            f"INSERT OR REPLACE INTO archive.claude_instances ({copy_cols}) "
            f"SELECT {copy_cols} FROM main.claude_instances"
        )

        cursor = await db.execute("SELECT count(*) FROM main.claude_instances")
        live_count = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT count(*) FROM archive.claude_instances")
        archive_count = (await cursor.fetchone())[0]
        if archive_count < live_count:
            raise RuntimeError(
                f"archive copy verification failed: {live_count} live rows, "
                f"{archive_count} archived"
            )
        await db.commit()
    finally:
        await db.execute("DETACH DATABASE archive")


async def _backfill_annex_from_legacy(db) -> None:
    """Fill runtime-annex (and NULL identity gaps) on live instance rows from legacy rows.

    instance identity always wins: persona_id/golden_throne are filled only when
    NULL. Legacy-only rows (no matching instances.id) stay archive-only.
    """
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute("SELECT * FROM claude_instances")
        legacy_rows = [dict(row) for row in await cursor.fetchall()]
    finally:
        db.row_factory = None

    legacy_cols = set(await _ordered_columns(db, "claude_instances"))
    annex_cols = [col for col in RUNTIME_ANNEX_COLUMNS if col in legacy_cols]

    for row in legacy_rows:
        instance_id = row.get("id")
        if not instance_id:
            continue
        cursor = await db.execute(
            "SELECT persona_id, golden_throne, status FROM instances WHERE id = ?",
            (instance_id,),
        )
        existing = await cursor.fetchone()
        if existing is None:
            continue  # legacy-only row: archive carries it, live does not

        updates: dict = {col: row.get(col) for col in annex_cols}

        existing_persona, existing_marker, existing_status = existing
        if existing_persona is None:
            persona_id = await _persona_id_for_legacy_row(db, row)
            if persona_id:
                updates["persona_id"] = persona_id

        if existing_marker is None:
            marker = golden_throne_binding(row)
            if marker:
                updates["golden_throne"] = marker
            elif (
                row.get("instance_type") or ""
            ).strip().lower() == "golden_throne" and existing_status not in ("stopped", "archived"):
                # Promote the legacy GT engine state into a golden_throne row
                # so the marker references real data (guard trigger enforces it).
                cursor = await db.execute(
                    """INSERT INTO golden_throne
                       (zealotry, resume_count, resume_window_started_at,
                        last_resume_at, follow_up_sop, stop_allowed)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        row.get("zealotry") or 4,
                        row.get("gt_resume_count") or 0,
                        row.get("gt_resume_window_started_at"),
                        row.get("gt_last_resume_at"),
                        row.get("follow_up_sop"),
                        1 if row.get("stop_allowed") in (None, 1) else 0,
                    ),
                )
                updates["golden_throne"] = str(cursor.lastrowid)

        if updates:
            assignments = ", ".join(f"{col} = ?" for col in updates)
            await db.execute(
                f"UPDATE instances SET {assignments} WHERE id = ?",
                [*updates.values(), instance_id],
            )


async def _rebuild_tables_without_legacy_fk(db) -> None:
    """Rebuild provenance tables whose DDL still REFERENCES claude_instances."""
    for table, canonical_ddl in _LEGACY_FK_REBUILDS.items():
        cursor = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        )
        found = await cursor.fetchone()
        if not found or "claude_instances" not in (found[0] or ""):
            continue
        await db.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy_fk")
        await db.execute(canonical_ddl)
        old_cols = await _ordered_columns(db, f"{table}_legacy_fk")
        new_cols = set(await _ordered_columns(db, table))
        copy_cols = ", ".join(col for col in old_cols if col in new_cols)
        await db.execute(
            f"INSERT INTO {table} ({copy_cols}) SELECT {copy_cols} FROM {table}_legacy_fk"
        )
        await db.execute(f"DROP TABLE {table}_legacy_fk")

    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND sql LIKE '%REFERENCES claude_instances%'"
    )
    stragglers = [row[0] for row in await cursor.fetchall()]
    if stragglers:
        raise RuntimeError(
            "tables still reference claude_instances after FK rebuild: " + ", ".join(stragglers)
        )


async def _extract_claude_instances(db, db_path: Path) -> None:
    """One-shot extraction: archive the legacy table, backfill annex, drop it.

    Idempotent (no-op once the table is gone) and reversible
    (restore_claude_instances_from_archive copies it back).
    """
    if not await _table_exists(db, "claude_instances"):
        return

    archive_path = archive_db_path_for(db_path)
    await _copy_legacy_table_to_archive(db, archive_path)
    await _backfill_annex_from_legacy(db)
    await _rebuild_tables_without_legacy_fk(db)
    await db.execute("DROP TABLE claude_instances")
    await db.commit()
    print(f"Extracted claude_instances to {archive_path} and dropped it from the live DB")


def restore_claude_instances_from_archive(db_path: Path | None = None) -> int:
    """Emergency reverse path: copy claude_instances back from archive.db.

    Returns the number of restored rows. Does NOT re-wire any code to read
    it — this exists so the extraction is operationally reversible.
    """
    db_path = db_path or DEFAULT_DB_PATH
    archive_path = archive_db_path_for(db_path)
    if not archive_path.exists():
        raise FileNotFoundError(f"no archive db at {archive_path}")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ATTACH DATABASE ? AS archive", (str(archive_path),))
        ddl_row = conn.execute(
            "SELECT sql FROM archive.sqlite_master WHERE type = 'table' AND name = 'claude_instances'"
        ).fetchone()
        if not ddl_row:
            raise RuntimeError(f"archive db {archive_path} has no claude_instances table")
        exists = conn.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = 'claude_instances'"
        ).fetchone()
        if not exists:
            ddl = ddl_row[0].replace("archive.claude_instances", "claude_instances")
            conn.execute(ddl)
        cols = [
            row[1] for row in conn.execute("PRAGMA archive.table_info(claude_instances)").fetchall()
        ]
        main_cols = {
            row[1] for row in conn.execute("PRAGMA main.table_info(claude_instances)").fetchall()
        }
        copy_cols = ", ".join(col for col in cols if col in main_cols)
        conn.execute(
            f"INSERT OR REPLACE INTO main.claude_instances ({copy_cols}) "
            f"SELECT {copy_cols} FROM archive.claude_instances"
        )
        count = conn.execute("SELECT count(*) FROM main.claude_instances").fetchone()[0]
        conn.commit()
        conn.execute("DETACH DATABASE archive")
        return count
    finally:
        conn.close()


async def init_database_async(db_path: Path | None = None) -> None:
    """Initialize the SQLite database with the managed schema and migrations."""
    db_path = db_path or DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await ensure_personas_table(db)

        # Legacy `claude_instances` is never created anymore. On a DB that
        # still carries it, run the last legacy persona repair (it reads the
        # legacy table), then extract: archive.db gets the data, live loses
        # the table. Fresh DBs skip all of this.
        if await _table_exists(db, "claude_instances"):
            repaired_personas = await _repair_legacy_instance_personas(db)
            if repaired_personas:
                print(f"Repaired {repaired_personas} legacy active persona assignments")

        await _ensure_instances(db)
        await _extract_claude_instances(db, db_path)

        # Annex-era indexes on the surviving predicates (golden_throne marker
        # replaced legion/synced; discord routing reads the annex columns).
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_instances_gt ON instances(golden_throne, status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_instances_discord ON instances(discord_channel, status)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                tailscale_ip TEXT UNIQUE,
                notification_method TEXT,
                webhook_url TEXT,
                tts_engine TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                instance_id TEXT,
                device_id TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(created_at DESC)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS state_injections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audience_instance_id TEXT NOT NULL,
                source_instance_id TEXT,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                rendered_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                consumed_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_state_injections_pending_audience
            ON state_injections(audience_instance_id, status, created_at)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS stop_hook_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_instance_id TEXT NOT NULL,
                target_pane TEXT,
                subscriber_instance_id TEXT,
                subscriber_pane TEXT NOT NULL,
                event TEXT NOT NULL DEFAULT 'stop',
                delivery TEXT NOT NULL DEFAULT 'prompt',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                unsubscribed_at TIMESTAMP,
                purpose TEXT NOT NULL DEFAULT 'generic',
                payload TEXT,
                oneshot INTEGER NOT NULL DEFAULT 0,
                UNIQUE(target_instance_id, subscriber_instance_id, subscriber_pane, event)
            )
        """)
        cursor = await db.execute("PRAGMA table_info(stop_hook_subscriptions)")
        sub_columns = {col[1] for col in await cursor.fetchall()}
        sub_migrations = [
            (
                "purpose",
                "ALTER TABLE stop_hook_subscriptions ADD COLUMN purpose TEXT NOT NULL DEFAULT 'generic'",
            ),
            ("payload", "ALTER TABLE stop_hook_subscriptions ADD COLUMN payload TEXT"),
            (
                "oneshot",
                "ALTER TABLE stop_hook_subscriptions ADD COLUMN oneshot INTEGER NOT NULL DEFAULT 0",
            ),
        ]
        for column_name, sql in sub_migrations:
            if column_name not in sub_columns:
                await db.execute(sql)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_stop_hook_subscriptions_active
            ON stop_hook_subscriptions(target_instance_id, event, status)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_stop_hook_subscriptions_purpose
            ON stop_hook_subscriptions(purpose, status)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stop_hook_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                target_instance_id TEXT NOT NULL,
                subscriber_instance_id TEXT,
                subscriber_pane TEXT NOT NULL,
                event TEXT NOT NULL DEFAULT 'stop',
                stop_event_key TEXT NOT NULL,
                delivery TEXT NOT NULL DEFAULT 'prompt',
                status TEXT NOT NULL,
                payload_json TEXT,
                pane_write_queue_id TEXT,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                delivered_at TIMESTAMP,
                UNIQUE(subscription_id, stop_event_key)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_stop_hook_deliveries_target
            ON stop_hook_deliveries(target_instance_id, created_at DESC)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS expected_acknowledgements (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                instance_id TEXT,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP NOT NULL,
                ack_due_at TIMESTAMP NOT NULL,
                level2_due_at TIMESTAMP NOT NULL,
                pavlok_due_at TIMESTAMP NOT NULL,
                acknowledged_at TIMESTAMP,
                bailout_reason TEXT,
                fired_levels_json TEXT DEFAULT '[]',
                details_json TEXT
            )
        """)
        cursor = await db.execute("PRAGMA table_info(expected_acknowledgements)")
        ack_columns = {col[1] for col in await cursor.fetchall()}
        if "fired_levels_json" not in ack_columns:
            await db.execute(
                "ALTER TABLE expected_acknowledgements ADD COLUMN fired_levels_json TEXT DEFAULT '[]'"
            )
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_expected_ack_pending
            ON expected_acknowledgements(status, ack_due_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_expected_ack_source_instance
            ON expected_acknowledgements(source, instance_id, status)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS pane_write_queue (
                id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                tmux_pane TEXT NOT NULL,
                source TEXT NOT NULL,
                purpose TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                attempted_at TIMESTAMP,
                sent_at TIMESTAMP,
                cancelled_at TIMESTAMP,
                last_error TEXT,
                last_result_json TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_pane_write_queue_pending
            ON pane_write_queue(status, created_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_pane_write_queue_instance_source
            ON pane_write_queue(instance_id, source, status)
        """)

        # Automated-activation markers. Every send through TmuxAdapter.run() is
        # automated by construction (token-api interventions / tmuxctl CLI /
        # enforcement / recovery — humans type directly into tmux, never through
        # run()). The send gate records a per-pane marker so compute_work_state
        # can discount the woken agent's reflex activity (instance last_activity
        # bump + work_action) from productivity accounting; otherwise an automated
        # state-hook/dispatch/enforcement wake re-anchors WORKING and the idle
        # clock never matures. PRIMARY KEY on tmux_pane ⇒ last-writer-wins upsert
        # slides the window forward across a multi-send reflex burst and bounds
        # the table by live pane count.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS automated_pane_activity (
                tmux_pane   TEXT PRIMARY KEY,
                injected_at TIMESTAMP NOT NULL,
                expires_at  TIMESTAMP NOT NULL,
                source      TEXT,
                verb        TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_automated_pane_activity_expires
            ON automated_pane_activity(expires_at)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_polls (
                poll_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                selector TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                PRIMARY KEY (poll_id, instance_id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_polls_poll
            ON pending_polls(poll_id, status)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_polls_instance
            ON pending_polls(instance_id, status)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_polls_expires
            ON pending_polls(expires_at)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS workflow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,
                workflow_state TEXT,
                event_type TEXT NOT NULL,
                event_owner TEXT,
                details_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_workflow_events_instance_time
            ON workflow_events(instance_id, created_at DESC)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_workflow_events_type_time
            ON workflow_events(event_type, created_at DESC)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS instance_mutations (
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_instance_mutations_instance_time
            ON instance_mutations(instance_id, created_at DESC)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_instance_mutations_write_txn
            ON instance_mutations(write_txn_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_instance_mutations_type_time
            ON instance_mutations(mutation_type, created_at DESC)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                task_type TEXT NOT NULL,
                schedule TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                max_retries INTEGER DEFAULT 0,
                retry_delay_seconds INTEGER DEFAULT 60,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TIMESTAMP NOT NULL,
                completed_at TIMESTAMP,
                duration_ms INTEGER,
                result TEXT,
                retry_count INTEGER DEFAULT 0,
                FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_executions_task_id ON task_executions(task_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_executions_started_at ON task_executions(started_at)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_locks (
                task_id TEXT PRIMARY KEY,
                locked_at TIMESTAMP NOT NULL,
                locked_by TEXT,
                FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS audio_proxy_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                phone_connected INTEGER DEFAULT 0,
                receiver_running INTEGER DEFAULT 0,
                receiver_pid INTEGER,
                last_connect_time TEXT,
                last_disconnect_time TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK (id = 1)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS timer_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS day_state (
                date TEXT PRIMARY KEY,
                day_started_at TEXT,
                source TEXT,
                details_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS timer_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                mode TEXT NOT NULL,
                duration_ms INTEGER DEFAULT 0,
                break_earned_ms INTEGER DEFAULT 0,
                break_used_ms INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS timer_mode_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP NOT NULL,
                old_mode TEXT,
                new_mode TEXT NOT NULL,
                is_automatic INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS timer_daily_scores (
                date TEXT PRIMARY KEY,
                productivity_score INTEGER,
                total_work_ms INTEGER DEFAULT 0,
                total_break_used_ms INTEGER DEFAULT 0,
                session_count INTEGER DEFAULT 0,
                mode_change_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checkin_type TEXT NOT NULL,
                date TEXT NOT NULL,
                energy INTEGER,
                focus INTEGER,
                mood TEXT,
                plan TEXT,
                notes TEXT,
                on_track INTEGER,
                source TEXT DEFAULT 'discord',
                prompted_at TIMESTAMP NOT NULL,
                responded_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(checkin_type, date)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS nudges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nudge_type TEXT NOT NULL,
                message TEXT NOT NULL,
                idle_minutes REAL,
                acknowledged INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS timer_shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                old_mode TEXT,
                new_mode TEXT NOT NULL,
                trigger TEXT,
                source TEXT,
                break_balance_ms INTEGER,
                break_backlog_ms INTEGER,
                work_time_ms INTEGER,
                active_instances INTEGER,
                phone_app TEXT,
                details TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS timer_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                mode TEXT NOT NULL,
                activity TEXT,
                productivity_active INTEGER,
                break_balance_ms INTEGER,
                break_backlog_ms INTEGER,
                work_time_ms INTEGER,
                active_instance_count INTEGER,
                processing_recent_count INTEGER,
                observed_agent_count INTEGER,
                desktop_mode TEXT,
                phone_app TEXT,
                source TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_timer_samples_timestamp ON timer_samples(timestamp)"
        )

        await CronEngine.init_tables(db)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS agent_state (
                id       TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guard_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cron_run_id INTEGER NOT NULL,
                job_id      TEXT NOT NULL,
                guard_index INTEGER NOT NULL,
                verdict     TEXT NOT NULL,
                findings    TEXT,
                model       TEXT DEFAULT 'MiniMax-M2.5',
                duration_ms INTEGER,
                created_at  TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS session_documents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path   TEXT NOT NULL UNIQUE,
                title       TEXT,
                project     TEXT,
                primarch_name TEXT,
                cron_job_id TEXT,
                status      TEXT DEFAULT 'active',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor = await db.execute("PRAGMA table_info(session_documents)")
        session_doc_columns = {col[1] for col in await cursor.fetchall()}
        session_doc_migrations = [
            ("primarch_name", "ALTER TABLE session_documents ADD COLUMN primarch_name TEXT"),
            ("cron_job_id", "ALTER TABLE session_documents ADD COLUMN cron_job_id TEXT"),
        ]
        for column_name, sql in session_doc_migrations:
            if column_name not in session_doc_columns:
                await db.execute(sql)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS primarch_session_docs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                primarch_name TEXT NOT NULL,
                session_doc_id INTEGER NOT NULL,
                linked_at     TEXT NOT NULL DEFAULT (datetime('now')),
                unlinked_at   TEXT,
                FOREIGN KEY (session_doc_id) REFERENCES session_documents(id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_primarch_active
              ON primarch_session_docs(primarch_name) WHERE unlinked_at IS NULL
        """)

        # ── Per-branch worktree registry (super-workflow Gap 1, D2 backstop) ──
        # DORMANT: merged as schema, not yet written to by any code path. Takes
        # effect only when init runs at startup. The partial-UNIQUE index is the
        # durable concurrency backstop behind worktree-setup's local-FS lock:
        # at most one active worktree per (project, branch) — 1 branch = 1
        # worktree = 1 PR. Matches the additive/idempotent house style and the
        # partial-index precedent above (idx_primarch_active).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS worktrees (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project     TEXT NOT NULL,
                branch      TEXT NOT NULL,
                path        TEXT NOT NULL,
                instance_id TEXT,
                dispatch_id TEXT,
                owner_pane  TEXT,
                status      TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'orphaned', 'quarantined', 'deleted')),
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                claimed_at  TIMESTAMP,
                last_seen_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_worktrees_active_unique
              ON worktrees(project, branch) WHERE status='active'
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS primarchs (
                name            TEXT PRIMARY KEY,
                title           TEXT NOT NULL,
                aliases         TEXT NOT NULL DEFAULT '[]',
                vault           TEXT NOT NULL,
                role            TEXT NOT NULL,
                instance_name_prefix TEXT NOT NULL,
                vault_note_path TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        primarch_seed = [
            (
                "vulkan",
                "Vulkan, The Promethean",
                '["v"]',
                "Imperium-ENV",
                "Infrastructure architect and system designer. Forges artifacts meant to outlast their maker. Primarch of the Vault Mind system.",
                "vulkan",
                "Personas/Vulkan.md",
            ),
            (
                "fabricator-general",
                "The Fabricator-General",
                '["fg", "fabricator"]',
                "Imperium-ENV",
                "Fleet orchestrator for the Mechanicus swarm. Reads state, detects stuck jobs, dispatches workers. The operational backbone of overnight automation.",
                "fabricator-general",
                "Personas/Fabricator-General.md",
            ),
            (
                "mechanicus",
                "Adeptus Mechanicus",
                '["mech", "mars"]',
                "Imperium-ENV",
                "Tech-priest worker. Builds, fixes, and maintains agent infrastructure. Takes assignments from Mars/Tasks/.",
                "mechanicus",
                "Personas/Mechanicus.md",
            ),
            (
                "administratum",
                "The Administratum",
                '["admin"]',
                "Imperium-ENV",
                "Background processor. Promotes completed session doc content into vault notes, then archives. The bridge between working memory and institutional memory.",
                "administratum",
                "Personas/Administratum.md",
            ),
            (
                "guilliman",
                "Guilliman, The Codifier",
                '["g", "guilliman", "ultramar"]',
                "Imperium-ENV",
                "Documentation Primarch. Takes raw knowledge and produces clean, cross-linked vault notes. Owns Terra/Ultramar/. Decides what is worth codifying and how to structure it.",
                "guilliman",
                "Personas/Guilliman.md",
            ),
            (
                "sanguinius",
                "Sanguinius, The Angel",
                '["sang", "sanguinius", "angel"]',
                "Imperium-ENV",
                "Prose stylist. Makes in-place edits to existing notes in Terra/Ultramar/ — elevates readability without changing meaning. Post-Guilliman polish pass.",
                "sanguinius",
                "Personas/Sanguinius.md",
            ),
            (
                "alpharius",
                "Alpharius, The Unknowable Twin",
                '["alpharius", "alpha", "hydra"]',
                "Imperium-ENV",
                "Deep reserve watchdog. Monitors fleet health, alerts on catastrophic failure. Reports through Mechanicus channels. I am Alpharius.",
                "alpharius",
                "Personas/Alpharius.md",
            ),
            (
                "dorn",
                "Dorn, The Imperial Fist",
                '["dorn", "fortify", "audit"]',
                "Imperium-ENV",
                "Security Primarch. Defensive auditor and hardening reviewer. Reviews code, infrastructure, and configurations for vulnerabilities. Does not build — inspects what others build before it ships.",
                "dorn",
                "Personas/Dorn.md",
            ),
            (
                "corax",
                "Corax, The Raven Lord",
                '["corax", "raven", "monitor", "codax"]',
                "Imperium-ENV",
                "Observability Primarch. Long-term monitoring, anomaly detection, pattern recognition across the entire system. Independent observer — not part of the Mechanicus command chain. Read-only. Silent by default, speaks when something is wrong.",
                "corax",
                "Personas/Corax.md",
            ),
            (
                "perturabo",
                "Perturabo, Lord of Iron",
                '["pert", "iron-within", "lord-of-iron"]',
                "Imperium-ENV",
                "Matters of the flesh. Food supply chain, meal prep logistics, inventory management, health telemetry. On-demand, not cron.",
                "perturabo",
                "Personas/Perturabo.md",
            ),
        ]
        for primarch in primarch_seed:
            await db.execute(
                """
                INSERT OR IGNORE INTO primarchs (name, title, aliases, vault, role, instance_name_prefix, vault_note_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                primarch,
            )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS habits (
                id                  TEXT PRIMARY KEY,
                name                TEXT NOT NULL,
                category            TEXT NOT NULL,
                window_start_hour   INTEGER NOT NULL,
                window_end_hour     INTEGER NOT NULL,
                notes               TEXT,
                active              INTEGER NOT NULL DEFAULT 1,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS habit_completions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                habit_id    TEXT NOT NULL REFERENCES habits(id),
                date        TEXT NOT NULL,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notes       TEXT,
                UNIQUE(habit_id, date)
            )
        """)

        # ── Legacy Pane Recolor System (retired) ─────────────────────
        # The DB triggers + 1s polling worker are gone. Pane tint is now applied
        # event-driven at lifecycle moments that actually change it (persona
        # register/change, pane rebind, vacate, close) from the managed table
        # instances.persona_id → personas.pane_tint. Disable the old writers but
        # do not drop the historical queue table/rows.
        await db.execute("DROP TRIGGER IF EXISTS trg_legion_recolor")
        await db.execute("DROP TRIGGER IF EXISTS trg_tmux_pane_recolor")

        # ── Pane State Queue (@CC_STATE) ──
        # Trigger-driven pane variable updates. Any status change on instances
        # queues a tmux set-option, so @CC_STATE stays in sync without caller
        # cooperation. Post-exterminatus these triggers live on `instances` and
        # push the instance status vocabulary (working/questioning/... not processing);
        # @CC_STATE consumers accept both vocabularies during the transition.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pane_state_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,
                variable TEXT NOT NULL,
                value TEXT NOT NULL,
                tmux_pane TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # DROP+CREATE (not IF NOT EXISTS): on upgraded DBs the old triggers of
        # the same name were bound to the extracted legacy table and died with it,
        # but mid-transition states must converge on the instances-bound ones.
        await db.execute("DROP TRIGGER IF EXISTS trg_status_pane_state")
        await db.execute("""
            CREATE TRIGGER trg_status_pane_state
            AFTER UPDATE OF status ON instances
            WHEN OLD.status IS NOT NEW.status
            BEGIN
                INSERT INTO pane_state_queue (instance_id, variable, value, tmux_pane)
                VALUES (NEW.id, '@CC_STATE', NEW.status, NEW.tmux_pane);
            END
        """)

        await db.execute("DROP TRIGGER IF EXISTS trg_planning_pane_state")
        await db.execute("""
            CREATE TRIGGER trg_planning_pane_state
            AFTER UPDATE OF planning_state ON instances
            WHEN OLD.planning_state IS NOT NEW.planning_state
            BEGIN
                INSERT INTO pane_state_queue (instance_id, variable, value, tmux_pane)
                VALUES (NEW.id, '@PLANNING_STATE', NEW.planning_state, NEW.tmux_pane);
            END
        """)

        # A rename queues the raw display name to @PANE_LABEL (Phase 1 Part A).
        # The pane border reads @PANE_LABEL in-format (zero fork per redraw) instead
        # of shelling out to tmux-pane-label every status-interval. Mirrors
        # trg_status_pane_state; pane_state_worker pushes it generically. Styling
        # stays in the format string — the var carries data, not presentation.
        # NEW.name IS NOT NULL guards the queue's NOT NULL value column: a rename
        # to NULL would otherwise abort the parent UPDATE on the constraint.
        await db.execute("DROP TRIGGER IF EXISTS trg_tab_name_pane_state")
        await db.execute("""
            CREATE TRIGGER trg_tab_name_pane_state
            AFTER UPDATE OF name ON instances
            WHEN OLD.name IS NOT NEW.name AND NEW.name IS NOT NULL
            BEGIN
                INSERT INTO pane_state_queue (instance_id, variable, value, tmux_pane)
                VALUES (NEW.id, '@PANE_LABEL', NEW.name, NEW.tmux_pane);
            END
        """)

        # ── Session Doc Sync Queue ──
        # Trigger-driven session doc frontmatter updates. Fires on status change,
        # tab rename, doc link, and doc unlink — keeps agents: list coherent.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS session_doc_sync_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id INTEGER NOT NULL,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # When status changes on an instance with a session doc, queue sync
        await db.execute("DROP TRIGGER IF EXISTS trg_doc_sync_status")
        await db.execute("""
            CREATE TRIGGER trg_doc_sync_status
            AFTER UPDATE OF status ON instances
            WHEN OLD.status IS NOT NEW.status AND NEW.session_doc_id IS NOT NULL
            BEGIN
                INSERT INTO session_doc_sync_queue (doc_id, reason)
                VALUES (NEW.session_doc_id, 'status_changed');
            END
        """)

        # When the display name changes, queue sync
        await db.execute("DROP TRIGGER IF EXISTS trg_doc_sync_rename")
        await db.execute("""
            CREATE TRIGGER trg_doc_sync_rename
            AFTER UPDATE OF name ON instances
            WHEN OLD.name IS NOT NEW.name AND NEW.session_doc_id IS NOT NULL
            BEGIN
                INSERT INTO session_doc_sync_queue (doc_id, reason)
                VALUES (NEW.session_doc_id, 'tab_renamed');
            END
        """)

        # When session_doc_id is set on an instance, queue sync for the new doc
        await db.execute("DROP TRIGGER IF EXISTS trg_doc_sync_linked")
        await db.execute("""
            CREATE TRIGGER trg_doc_sync_linked
            AFTER UPDATE OF session_doc_id ON instances
            WHEN NEW.session_doc_id IS NOT NULL AND (OLD.session_doc_id IS NULL OR OLD.session_doc_id != NEW.session_doc_id)
            BEGIN
                INSERT INTO session_doc_sync_queue (doc_id, reason)
                VALUES (NEW.session_doc_id, 'doc_linked');
            END
        """)

        # When session_doc_id is cleared, queue sync for the OLD doc
        await db.execute("DROP TRIGGER IF EXISTS trg_doc_sync_unlinked")
        await db.execute("""
            CREATE TRIGGER trg_doc_sync_unlinked
            AFTER UPDATE OF session_doc_id ON instances
            WHEN OLD.session_doc_id IS NOT NULL AND (NEW.session_doc_id IS NULL OR OLD.session_doc_id != NEW.session_doc_id)
            BEGIN
                INSERT INTO session_doc_sync_queue (doc_id, reason)
                VALUES (OLD.session_doc_id, 'doc_unlinked');
            END
        """)

        device_seed = [
            ("Mac-Mini", "Mac Mini", "local", "100.95.109.23", "tts_sound", None, "macos_say"),
            ("desktop", "Desktop", "local", "100.66.10.74", "tts_sound", None, "windows_sapi"),
            ("TokenPC", "Token PC", "local", "100.69.198.87", "tts_sound", None, "windows_sapi"),
            (
                "Token-S24",
                "Pixel Phone",
                "mobile",
                "100.102.92.24",
                "webhook",
                "http://100.102.92.24:7777/notify",
                None,
            ),
        ]
        for (
            device_id,
            name,
            device_type,
            tailscale_ip,
            notify_method,
            webhook_url,
            tts_engine,
        ) in device_seed:
            await db.execute(
                """
                INSERT OR IGNORE INTO devices (id, name, type, tailscale_ip, notification_method, webhook_url, tts_engine)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    device_id,
                    name,
                    device_type,
                    tailscale_ip,
                    notify_method,
                    webhook_url,
                    tts_engine,
                ),
            )

        scheduled_task_seed = [
            (
                "cleanup_stale_instances",
                "Cleanup Stale Instances",
                "Mark instances with no activity for 3+ hours as stopped",
                "interval",
                "30m",
                2,
            ),
            (
                "purge_old_events",
                "Purge Old Events",
                "Delete events older than 30 days",
                "cron",
                "0 3 * * *",
                1,
            ),
            (
                "morning_supervisor_arm",
                "Morning Supervisor Arm",
                "Bookkeeping check (the only fixed day-start cron): derive expected wake "
                "empirically from the last same-type real ack and arm the relative "
                "morning-session watchdog poller. The reactive day-start stays "
                "event-driven (alarm_silenced); there is no magic-number wake cron.",
                "cron",
                "0 4 * * *",
                0,
            ),
        ]
        for task_id, name, description, task_type, schedule, max_retries in scheduled_task_seed:
            await db.execute(
                """
                INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (task_id, name, description, task_type, schedule, max_retries),
            )

        # Retire the legacy magic-number day-start fallback. There is no fixed
        # wake-anchor cron anymore: the reactive day-start is event-driven
        # (alarm_silenced), and "expected wake" lives only in the morning
        # supervisor. Drop any pre-existing row so an older DB stops firing the
        # phantom 08:30 morning session. History in task_executions is retained.
        await db.execute("DELETE FROM scheduled_tasks WHERE id = 'day_start_schedule_fallback'")
        await db.execute("DELETE FROM task_locks WHERE task_id = 'day_start_schedule_fallback'")

        checkin_tasks = [
            (
                "checkin_morning_start",
                "Morning Start Check-in",
                "Energy, focus, mood, and today's focus",
                "0 9 * * 1-5",
            ),
            (
                "checkin_mid_morning",
                "Mid-Morning Check-in",
                "Focus check and on-track status",
                "30 10 * * 1-5",
            ),
            (
                "checkin_decision_point",
                "Decision Point Check-in",
                "Gym or power through, energy check",
                "0 11 * * 1-5",
            ),
            (
                "checkin_afternoon",
                "Afternoon Start Check-in",
                "Energy and focus after lunch",
                "0 13 * * 1-5",
            ),
            (
                "checkin_afternoon_check",
                "Afternoon Check",
                "Energy, focus, and need help assessment",
                "30 14 * * 1-5",
            ),
        ]
        for task_id, name, description, schedule in checkin_tasks:
            await db.execute(
                """
                INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
                VALUES (?, ?, ?, 'cron', ?, 0)
            """,
                (task_id, name, description, schedule),
            )

        default_habits = [
            ("morning_teeth", "Brush teeth", "morning", 6, 10, None),
            ("morning_breakfast", "Breakfast", "morning", 6, 11, None),
            (
                "morning_movement",
                "Morning movement",
                "morning",
                6,
                11,
                "Stretch, walk, or exercise",
            ),
            ("work_deep_work", "Deep work session", "work", 9, 14, "At least one focused block"),
            ("work_calendar", "Calendar review", "work", 9, 13, None),
            ("health_gym", "Gym / exercise", "health", 9, 21, None),
            ("health_water", "Hydration", "health", 6, 22, "Drink water throughout the day"),
            ("evening_reflection", "Evening reflection", "evening", 19, 24, None),
            ("evening_reading", "Reading", "evening", 19, 24, None),
            (
                "evening_tomorrow",
                "Tomorrow prep",
                "evening",
                19,
                24,
                "Review tomorrow's calendar and tasks",
            ),
        ]
        for habit in default_habits:
            await db.execute(
                """
                INSERT OR IGNORE INTO habits (id, name, category, window_start_hour, window_end_hour, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                habit,
            )

        await db.commit()
        print(f"Database initialized at {db_path}")


def init_database_sync(db_path: Path | None = None) -> None:
    """Synchronous wrapper for the canonical async DB initialization."""
    asyncio.run(init_database_async(db_path))
