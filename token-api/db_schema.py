"""
Canonical SQLite schema and migrations for Token-API.

All bootstrap paths should go through this module:
- FastAPI startup
- standalone init_db.py
- tests
"""

import asyncio
import os
from pathlib import Path

import aiosqlite

from cron_engine import CronEngine


DEFAULT_DB_PATH = Path(os.environ.get("TOKEN_API_DB", Path.home() / ".claude" / "agents.db"))


async def init_database_async(db_path: Path | None = None) -> None:
    """Initialize the SQLite database with the canonical schema and migrations."""
    db_path = db_path or DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS claude_instances (
                id TEXT PRIMARY KEY,
                session_id TEXT UNIQUE NOT NULL,
                tab_name TEXT,
                working_dir TEXT,
                origin_type TEXT NOT NULL,
                source_ip TEXT,
                device_id TEXT NOT NULL,
                profile_name TEXT,
                tts_voice TEXT,
                notification_sound TEXT,
                pid INTEGER,
                status TEXT DEFAULT 'idle',
                is_processing INTEGER DEFAULT 0,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                stopped_at TIMESTAMP
            )
        """)

        cursor = await db.execute("PRAGMA table_info(claude_instances)")
        columns = {col[1] for col in await cursor.fetchall()}
        instance_migrations = [
            ("is_processing", "ALTER TABLE claude_instances ADD COLUMN is_processing INTEGER DEFAULT 0"),
            ("working_dir", "ALTER TABLE claude_instances ADD COLUMN working_dir TEXT"),
            ("is_subagent", "ALTER TABLE claude_instances ADD COLUMN is_subagent INTEGER DEFAULT 0"),
            ("spawner", "ALTER TABLE claude_instances ADD COLUMN spawner TEXT"),
            ("tts_mode", "ALTER TABLE claude_instances ADD COLUMN tts_mode TEXT DEFAULT 'verbose'"),
            ("session_doc_id", "ALTER TABLE claude_instances ADD COLUMN session_doc_id INTEGER"),
            ("zealotry", "ALTER TABLE claude_instances ADD COLUMN zealotry INTEGER DEFAULT 4"),
            ("tmux_pane", "ALTER TABLE claude_instances ADD COLUMN tmux_pane TEXT"),
            ("victory_at", "ALTER TABLE claude_instances ADD COLUMN victory_at TIMESTAMP"),
            ("victory_reason", "ALTER TABLE claude_instances ADD COLUMN victory_reason TEXT"),
            ("input_lock", "ALTER TABLE claude_instances ADD COLUMN input_lock TEXT"),
            ("primarch", "ALTER TABLE claude_instances ADD COLUMN primarch TEXT"),
            ("transplant_target_session", "ALTER TABLE claude_instances ADD COLUMN transplant_target_session TEXT"),
            ("legion", "ALTER TABLE claude_instances ADD COLUMN legion TEXT DEFAULT 'astartes'"),
            ("synced", "ALTER TABLE claude_instances ADD COLUMN synced INTEGER DEFAULT 0"),
            ("discord_hosted", "ALTER TABLE claude_instances ADD COLUMN discord_hosted INTEGER DEFAULT 0"),
            ("discord_channel", "ALTER TABLE claude_instances ADD COLUMN discord_channel TEXT"),
            ("follow_up_sop", "ALTER TABLE claude_instances ADD COLUMN follow_up_sop TEXT"),
            ("instance_type", "ALTER TABLE claude_instances ADD COLUMN instance_type TEXT DEFAULT 'one_off'"),
            ("pane_label", "ALTER TABLE claude_instances ADD COLUMN pane_label TEXT"),
            ("pre_stop_status", "ALTER TABLE claude_instances ADD COLUMN pre_stop_status TEXT"),
            ("retrigger_count", "ALTER TABLE claude_instances ADD COLUMN retrigger_count INTEGER DEFAULT 0"),
        ]
        for column_name, sql in instance_migrations:
            if column_name not in columns:
                await db.execute(sql)

        if "instance_type" not in columns:
            await db.execute("""UPDATE claude_instances SET instance_type = CASE
                WHEN synced = 1 AND status IN ('processing', 'idle') THEN 'sync'
                WHEN victory_at IS NOT NULL THEN 'one_off'
                WHEN zealotry >= 4 AND COALESCE(is_subagent, 0) = 0 THEN 'golden_throne'
                ELSE 'one_off'
            END""")

        cursor = await db.execute("SELECT COUNT(*) FROM claude_instances WHERE status = 'active'")
        if (await cursor.fetchone())[0] > 0:
            await db.execute("""
                UPDATE claude_instances SET status = CASE
                    WHEN status = 'active' AND is_processing = 1 THEN 'processing'
                    WHEN status = 'active' AND is_processing = 0 THEN 'idle'
                    ELSE status
                END
            """)
            await db.commit()

        await db.execute("CREATE INDEX IF NOT EXISTS idx_instances_status ON claude_instances(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_instances_device ON claude_instances(device_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_instances_legion_synced ON claude_instances(legion, synced, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_instances_discord ON claude_instances(discord_channel, status)")

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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_task_id ON task_executions(task_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_started_at ON task_executions(started_at)")

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
            ("vulkan", "Vulkan, The Promethean", '["v"]', "Imperium-ENV", "Infrastructure architect and system designer. Forges artifacts meant to outlast their maker. Primarch of the Vault Mind system.", "vulkan", "Personas/Vulkan.md"),
            ("fabricator-general", "The Fabricator-General", '["fg", "fabricator"]', "Imperium-ENV", "Fleet orchestrator for the Mechanicus swarm. Reads state, detects stuck jobs, dispatches workers. The operational backbone of overnight automation.", "fabricator-general", "Personas/Fabricator-General.md"),
            ("mechanicus", "Adeptus Mechanicus", '["mech", "mars"]', "Imperium-ENV", "Tech-priest worker. Builds, fixes, and maintains agent infrastructure. Takes assignments from Mars/Tasks/.", "mechanicus", "Personas/Mechanicus.md"),
            ("administratum", "The Administratum", '["admin"]', "Imperium-ENV", "Background processor. Promotes completed session doc content into vault notes, then archives. The bridge between working memory and institutional memory.", "administratum", "Personas/Administratum.md"),
            ("guilliman", "Guilliman, The Codifier", '["g", "guilliman", "ultramar"]', "Imperium-ENV", "Documentation Primarch. Takes raw knowledge and produces clean, cross-linked vault notes. Owns Terra/Ultramar/. Decides what is worth codifying and how to structure it.", "guilliman", "Personas/Guilliman.md"),
            ("sanguinius", "Sanguinius, The Angel", '["sang", "sanguinius", "angel"]', "Imperium-ENV", "Prose stylist. Makes in-place edits to existing notes in Terra/Ultramar/ — elevates readability without changing meaning. Post-Guilliman polish pass.", "sanguinius", "Personas/Sanguinius.md"),
            ("alpharius", "Alpharius, The Unknowable Twin", '["alpharius", "alpha", "hydra"]', "Imperium-ENV", "Deep reserve watchdog. Monitors fleet health, alerts on catastrophic failure. Reports through Mechanicus channels. I am Alpharius.", "alpharius", "Personas/Alpharius.md"),
            ("dorn", "Dorn, The Imperial Fist", '["dorn", "fortify", "audit"]', "Imperium-ENV", "Security Primarch. Defensive auditor and hardening reviewer. Reviews code, infrastructure, and configurations for vulnerabilities. Does not build — inspects what others build before it ships.", "dorn", "Personas/Dorn.md"),
            ("corax", "Corax, The Raven Lord", '["corax", "raven", "monitor", "codax"]', "Imperium-ENV", "Observability Primarch. Long-term monitoring, anomaly detection, pattern recognition across the entire system. Independent observer — not part of the Mechanicus command chain. Read-only. Silent by default, speaks when something is wrong.", "corax", "Personas/Corax.md"),
            ("perturabo", "Perturabo, Lord of Iron", '["pert", "iron-within", "lord-of-iron"]', "Imperium-ENV", "Matters of the flesh. Food supply chain, meal prep logistics, inventory management, health telemetry. On-demand, not cron.", "perturabo", "Personas/Perturabo.md"),
        ]
        for primarch in primarch_seed:
            await db.execute("""
                INSERT OR IGNORE INTO primarchs (name, title, aliases, vault, role, instance_name_prefix, vault_note_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, primarch)

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

        # ── Legion Pane Recolor System ──────────────────────────────
        # Queue table: background processor reads this and applies tmux pane colors.
        # SQLite trigger fires on ANY legion column update, catching all entry points.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pane_recolor_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,
                legion TEXT NOT NULL,
                tmux_pane TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Trigger: whenever legion changes on an instance, queue a recolor
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_legion_recolor
            AFTER UPDATE OF legion ON claude_instances
            WHEN OLD.legion IS NOT NEW.legion
               OR (OLD.legion IS NULL AND NEW.legion IS NOT NULL)
            BEGIN
                INSERT INTO pane_recolor_queue (instance_id, legion, tmux_pane)
                VALUES (NEW.id, NEW.legion, NEW.tmux_pane);
            END
        """)

        # ── Pane State Queue (@CC_STATE) ──
        # Trigger-driven pane variable updates. Any status change on claude_instances
        # queues a tmux set-option, so @CC_STATE stays in sync without caller cooperation.
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

        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_status_pane_state
            AFTER UPDATE OF status ON claude_instances
            WHEN OLD.status IS NOT NEW.status
            BEGIN
                INSERT INTO pane_state_queue (instance_id, variable, value, tmux_pane)
                VALUES (NEW.id, '@CC_STATE', NEW.status, NEW.tmux_pane);
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
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_doc_sync_status
            AFTER UPDATE OF status ON claude_instances
            WHEN OLD.status IS NOT NEW.status AND NEW.session_doc_id IS NOT NULL
            BEGIN
                INSERT INTO session_doc_sync_queue (doc_id, reason)
                VALUES (NEW.session_doc_id, 'status_changed');
            END
        """)

        # When tab_name changes, queue sync
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_doc_sync_rename
            AFTER UPDATE OF tab_name ON claude_instances
            WHEN OLD.tab_name IS NOT NEW.tab_name AND NEW.session_doc_id IS NOT NULL
            BEGIN
                INSERT INTO session_doc_sync_queue (doc_id, reason)
                VALUES (NEW.session_doc_id, 'tab_renamed');
            END
        """)

        # When session_doc_id is set on an instance, queue sync for the new doc
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_doc_sync_linked
            AFTER UPDATE OF session_doc_id ON claude_instances
            WHEN NEW.session_doc_id IS NOT NULL AND (OLD.session_doc_id IS NULL OR OLD.session_doc_id != NEW.session_doc_id)
            BEGIN
                INSERT INTO session_doc_sync_queue (doc_id, reason)
                VALUES (NEW.session_doc_id, 'doc_linked');
            END
        """)

        # When session_doc_id is cleared, queue sync for the OLD doc
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_doc_sync_unlinked
            AFTER UPDATE OF session_doc_id ON claude_instances
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
            ("Token-S24", "Pixel Phone", "mobile", "100.102.92.24", "webhook", "http://100.102.92.24:7777/notify", None),
        ]
        for device_id, name, device_type, tailscale_ip, notify_method, webhook_url, tts_engine in device_seed:
            await db.execute("""
                INSERT OR IGNORE INTO devices (id, name, type, tailscale_ip, notification_method, webhook_url, tts_engine)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (device_id, name, device_type, tailscale_ip, notify_method, webhook_url, tts_engine))

        scheduled_task_seed = [
            ("cleanup_stale_instances", "Cleanup Stale Instances", "Mark instances with no activity for 3+ hours as stopped", "interval", "30m", 2),
            ("purge_old_events", "Purge Old Events", "Delete events older than 30 days", "cron", "0 3 * * *", 1),
        ]
        for task_id, name, description, task_type, schedule, max_retries in scheduled_task_seed:
            await db.execute("""
                INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (task_id, name, description, task_type, schedule, max_retries))

        checkin_tasks = [
            ("checkin_morning_start", "Morning Start Check-in", "Energy, focus, mood, and today's focus", "0 9 * * 1-5"),
            ("checkin_mid_morning", "Mid-Morning Check-in", "Focus check and on-track status", "30 10 * * 1-5"),
            ("checkin_decision_point", "Decision Point Check-in", "Gym or power through, energy check", "0 11 * * 1-5"),
            ("checkin_afternoon", "Afternoon Start Check-in", "Energy and focus after lunch", "0 13 * * 1-5"),
            ("checkin_afternoon_check", "Afternoon Check", "Energy, focus, and need help assessment", "30 14 * * 1-5"),
        ]
        for task_id, name, description, schedule in checkin_tasks:
            await db.execute("""
                INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
                VALUES (?, ?, ?, 'cron', ?, 0)
            """, (task_id, name, description, schedule))

        default_habits = [
            ("morning_teeth", "Brush teeth", "morning", 6, 10, None),
            ("morning_breakfast", "Breakfast", "morning", 6, 11, None),
            ("morning_movement", "Morning movement", "morning", 6, 11, "Stretch, walk, or exercise"),
            ("work_deep_work", "Deep work session", "work", 9, 14, "At least one focused block"),
            ("work_calendar", "Calendar review", "work", 9, 13, None),
            ("health_gym", "Gym / exercise", "health", 9, 21, None),
            ("health_water", "Hydration", "health", 6, 22, "Drink water throughout the day"),
            ("evening_reflection", "Evening reflection", "evening", 19, 24, None),
            ("evening_reading", "Reading", "evening", 19, 24, None),
            ("evening_tomorrow", "Tomorrow prep", "evening", 19, 24, "Review tomorrow's calendar and tasks"),
        ]
        for habit in default_habits:
            await db.execute("""
                INSERT OR IGNORE INTO habits (id, name, category, window_start_hour, window_end_hour, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, habit)

        await db.commit()
        print(f"Database initialized at {db_path}")


def init_database_sync(db_path: Path | None = None) -> None:
    """Synchronous wrapper for the canonical async DB initialization."""
    asyncio.run(init_database_async(db_path))
