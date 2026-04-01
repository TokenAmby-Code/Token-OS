#!/usr/bin/env python3
"""
Initialize the SQLite database with required tables and seed data.
Run this script standalone or let the FastAPI app initialize on startup.
"""

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("TOKEN_API_DB", Path.home() / ".claude" / "agents.db"))


def init_database():
    """Initialize SQLite database with required tables."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Enable WAL mode for concurrent read/write access
    # This prevents TUI reads from blocking server writes
    cursor.execute("PRAGMA journal_mode=WAL")

    # Set busy timeout to 5 seconds (prevents indefinite blocking on lock contention)
    cursor.execute("PRAGMA busy_timeout=5000")

    # Create claude_instances table
    cursor.execute("""
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

    # Migration: add is_processing column if it doesn't exist
    cursor.execute("PRAGMA table_info(claude_instances)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'is_processing' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN is_processing INTEGER DEFAULT 0")
    if 'working_dir' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN working_dir TEXT")
    if 'is_subagent' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN is_subagent INTEGER DEFAULT 0")
    if 'spawner' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN spawner TEXT")
    if 'tts_mode' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN tts_mode TEXT DEFAULT 'verbose'")
    if 'session_doc_id' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN session_doc_id INTEGER")
    if 'zealotry' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN zealotry INTEGER DEFAULT 4")
    if 'tmux_pane' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN tmux_pane TEXT")
    if 'victory_at' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN victory_at TIMESTAMP")
    if 'victory_reason' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN victory_reason TEXT")
    if 'primarch' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN primarch TEXT")
    if 'legion' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN legion TEXT DEFAULT 'astartes'")
    if 'synced' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN synced INTEGER DEFAULT 0")
    if 'instance_type' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN instance_type TEXT DEFAULT 'one_off'")
        cursor.execute("""UPDATE claude_instances SET instance_type = CASE
            WHEN synced = 1 AND status IN ('processing', 'idle') THEN 'sync'
            WHEN victory_at IS NOT NULL THEN 'one_off'
            WHEN zealotry >= 4 AND COALESCE(is_subagent, 0) = 0 THEN 'golden_throne'
            ELSE 'one_off'
        END""")

    # Migration: Convert two-field status (status + is_processing) to single enum
    # Old: status='active' + is_processing=0/1 → New: status='processing'/'idle'/'stopped'
    cursor.execute("SELECT COUNT(*) FROM claude_instances WHERE status = 'active'")
    if cursor.fetchone()[0] > 0:
        cursor.execute("""
            UPDATE claude_instances SET status = CASE
                WHEN status = 'active' AND is_processing = 1 THEN 'processing'
                WHEN status = 'active' AND is_processing = 0 THEN 'idle'
                ELSE status
            END
        """)
        conn.commit()

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_instances_status ON claude_instances(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_instances_device ON claude_instances(device_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_instances_legion_synced ON claude_instances(legion, synced, status)")

    # Create devices table
    cursor.execute("""
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

    # Create events table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            instance_id TEXT,
            device_id TEXT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(created_at DESC)")

    # Create scheduled_tasks table
    cursor.execute("""
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

    # Create task_executions table
    cursor.execute("""
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

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_task_id ON task_executions(task_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_started_at ON task_executions(started_at)")

    # Create task_locks table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_locks (
            task_id TEXT PRIMARY KEY,
            locked_at TIMESTAMP NOT NULL,
            locked_by TEXT,
            FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
        )
    """)

    # Create audio_proxy_state table (for phone audio routing through PC)
    cursor.execute("""
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

    # Create timer_state table (single-row, stores timer engine state as JSON)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS timer_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            state_json TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create agent_state table (generic state blob per agent type)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_state (
            id       TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # Create guard_runs table (Imperial Guards post-run validation results)
    cursor.execute("""
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

    # Create session_documents table (persistent Obsidian notes linked to instances)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_documents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path   TEXT NOT NULL UNIQUE,
            title       TEXT,
            project     TEXT,
            cron_job_id TEXT,
            status      TEXT DEFAULT 'active',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migration: add cron_job_id to session_documents
    cursor.execute("PRAGMA table_info(session_documents)")
    sd_columns = [col[1] for col in cursor.fetchall()]
    if 'cron_job_id' not in sd_columns:
        cursor.execute("ALTER TABLE session_documents ADD COLUMN cron_job_id TEXT")

    # Create primarchs table (registry of primarch identities)
    cursor.execute("""
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

    # Seed primarchs
    primarch_seed = [
        ("vulkan", "Vulkan, The Promethean", '["v"]', "Imperium-ENV", "Infrastructure architect and system designer. Forges artifacts meant to outlast their maker. Primarch of the Vault Mind system.", "vulkan", "Personas/Vulkan.md"),
        ("fabricator-general", "The Fabricator-General", '["fg", "fabricator"]', "Imperium-ENV", "Fleet orchestrator for the Mechanicus swarm. Reads state, detects stuck jobs, dispatches workers. The operational backbone of overnight automation.", "fabricator-general", "Personas/Fabricator-General.md"),
        ("mechanicus", "Adeptus Mechanicus", '["mech", "mars"]', "Imperium-ENV", "Tech-priest worker. Builds, fixes, and maintains agent infrastructure. Takes assignments from Mars/Tasks/.", "mechanicus", "Personas/Mechanicus.md"),
        ("administratum", "The Administratum", '["admin"]', "Imperium-ENV", "Background processor. Promotes completed session doc content into vault notes, then archives. The bridge between working memory and institutional memory.", "administratum", "Personas/Administratum.md"),
        ("guilliman", "Guilliman, The Codifier", '["g", "guilliman", "ultramar"]', "Imperium-ENV", "Documentation Primarch. Takes raw knowledge and produces clean, cross-linked vault notes. Owns Terra/Ultramar/. Decides what is worth codifying and how to structure it.", "guilliman", "Personas/Guilliman.md"),
        ("sanguinius", "Sanguinius, The Angel", '["sang", "sanguinius", "angel"]', "Imperium-ENV", "Prose stylist. Makes in-place edits to existing notes in Terra/Ultramar/ — elevates readability without changing meaning. Post-Guilliman polish pass.", "sanguinius", "Personas/Sanguinius.md"),
        ("dorn", "Dorn, The Imperial Fist", '["dorn", "fortify", "audit"]', "Imperium-ENV", "Security Primarch. Defensive auditor and hardening reviewer. Reviews code, infrastructure, and configurations for vulnerabilities. Does not build — inspects what others build before it ships.", "dorn", "Personas/Dorn.md"),
        ("corax", "Corax, The Raven Lord", '["corax", "raven", "monitor", "codax"]', "Imperium-ENV", "Observability Primarch. Long-term monitoring, anomaly detection, pattern recognition across the entire system. Independent observer — not part of the Mechanicus command chain. Read-only. Silent by default, speaks when something is wrong.", "corax", "Personas/Corax.md"),
    ]
    for p in primarch_seed:
        cursor.execute("""
            INSERT OR IGNORE INTO primarchs (name, title, aliases, vault, role, instance_name_prefix, vault_note_path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, p)

    # Seed devices
    cursor.execute("""
        INSERT OR IGNORE INTO devices (id, name, type, tailscale_ip, notification_method, tts_engine)
        VALUES ('Mac-Mini', 'Mac Mini', 'local', '100.95.109.23', 'tts_sound', 'macos_say')
    """)

    cursor.execute("""
        INSERT OR IGNORE INTO devices (id, name, type, tailscale_ip, notification_method, webhook_url)
        VALUES ('Token-S24', 'Pixel Phone', 'mobile', '100.102.92.24', 'webhook', 'http://100.102.92.24:7777/notify')
    """)

    # Seed scheduled tasks
    cursor.execute("""
        INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
        VALUES ('cleanup_stale_instances', 'Cleanup Stale Instances',
                'Mark instances with no activity for 3+ hours as stopped',
                'interval', '30m', 2)
    """)

    cursor.execute("""
        INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
        VALUES ('purge_old_events', 'Purge Old Events',
                'Delete events older than 30 days',
                'cron', '0 3 * * *', 1)
    """)

    conn.commit()
    conn.close()

    print(f"Database initialized at {DB_PATH}")
    print("Tables created: claude_instances, devices, events, scheduled_tasks, task_executions, task_locks, audio_proxy_state")
    print("Devices seeded: Mac-Mini, Token-S24")
    print("Tasks seeded: cleanup_stale_instances, purge_old_events")


if __name__ == "__main__":
    init_database()
