from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "tmux-pane-label"


def _make_db(path: Path, *, status: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE claude_instances (
                id TEXT PRIMARY KEY,
                tab_name TEXT,
                engine TEXT,
                zealotry INTEGER,
                tmux_pane TEXT,
                status TEXT,
                last_activity TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO claude_instances
                (id, tab_name, engine, zealotry, tmux_pane, status, last_activity)
            VALUES
                ('abc123', 'Old Agent', 'claude', 4, '%1', ?, '2026-05-05T12:00:00')
            """,
            (status,),
        )
        conn.commit()
    finally:
        conn.close()


def _run_label(tmp_path: Path, db: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "AGENTS_DB": str(db),
        "APSCHEDULER_DB": str(tmp_path / "missing-scheduler.db"),
        "TMUX_PANE_LABEL_CACHE": str(tmp_path / "cache"),
        "TOKEN_API_URL": "http://127.0.0.1:9",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), "%1"],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_active_instance_renders_label(tmp_path: Path) -> None:
    db = tmp_path / "agents.db"
    _make_db(db, status="idle")

    proc = _run_label(tmp_path, db)

    assert proc.returncode == 0
    assert "Old Agent" in proc.stdout


def test_stopped_instance_does_not_render_or_keep_cached_label(tmp_path: Path) -> None:
    db = tmp_path / "agents.db"
    _make_db(db, status="stopped")
    cache_file = tmp_path / "cache" / "1"
    cache_file.parent.mkdir()
    cache_file.write_text("stale label")

    proc = _run_label(tmp_path, db)

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not cache_file.exists()
