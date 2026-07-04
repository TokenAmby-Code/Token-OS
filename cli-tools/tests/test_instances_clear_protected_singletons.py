from __future__ import annotations

import os
import pathlib
import sqlite3
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(REPO_ROOT / "tmuxctld" / "lib"))

from tmuxctl.singleton_labels import PERSONA_SINGLETON_LABELS

INSTANCES_CLEAR = ROOT / "bin" / "instances-clear"


def _seed_db(path: pathlib.Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE personas (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL
        );
        CREATE TABLE instances (
            id TEXT PRIMARY KEY,
            name TEXT,
            status TEXT,
            created_at TEXT,
            stopped_at TEXT,
            persona_id TEXT,
            pane_label TEXT
        );
        """
    )
    for index, label in enumerate(sorted(PERSONA_SINGLETON_LABELS), start=1):
        slug = label.rsplit(":", 1)[-1]
        persona_id = f"persona-{index}"
        conn.execute(
            "INSERT INTO personas (id, slug) VALUES (?, ?)",
            (persona_id, slug),
        )
        conn.execute(
            """
            INSERT INTO instances
                (id, name, status, created_at, stopped_at, persona_id, pane_label)
            VALUES (?, ?, 'stopped', '2026-07-03T00:00:00', '2026-07-03T00:01:00', ?, ?)
            """,
            (f"protected-{label}", label, persona_id, label),
        )
    conn.execute(
        """
        INSERT INTO instances
            (id, name, status, created_at, stopped_at, persona_id, pane_label)
        VALUES
            ('ordinary-stopped', 'ordinary worker', 'stopped',
             '2026-07-03T00:00:00', '2026-07-03T00:01:00', NULL, 'mechanicus:42')
        """
    )
    conn.commit()
    conn.close()


def test_instances_clear_preserves_all_canonical_singletons_and_deletes_ordinary(tmp_path):
    db_path = tmp_path / "agents.db"
    _seed_db(db_path)

    env = dict(os.environ)
    env["TOKEN_API_AGENTS_DB"] = str(db_path)

    subprocess.run(
        [sys.executable, str(INSTANCES_CLEAR), "--confirm"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    conn = sqlite3.connect(db_path)
    rows = {
        row[0]: row[1] for row in conn.execute("SELECT id, pane_label FROM instances ORDER BY id")
    }
    conn.close()

    assert "ordinary-stopped" not in rows
    for label in PERSONA_SINGLETON_LABELS:
        assert rows[f"protected-{label}"] == label
