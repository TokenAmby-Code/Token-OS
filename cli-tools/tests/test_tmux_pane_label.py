"""tmux-pane-label is retired from the pane-border hot path (Phase 1 Part A).

It now survives only as a deploy-time `--backfill` that seeds @PANE_LABEL on panes
that pre-date the push path, and the legacy positional invocation is a deliberate
no-op so a not-yet-reloaded tmux config cannot error or spawn work.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "tmux-pane-label"


def _make_db(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """rows: (id, tab_name, tmux_pane, status)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE claude_instances (
                id TEXT PRIMARY KEY,
                tab_name TEXT,
                tmux_pane TEXT,
                status TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO claude_instances (id, tab_name, tmux_pane, status) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _fake_tmux(tmp_path: Path) -> tuple[Path, Path]:
    """A fake `tmux` on PATH that logs each invocation's args to a file."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "tmux-calls.log"
    fake = bindir / "tmux"
    fake.write_text('#!/bin/sh\necho "$*" >> "$TMUX_CALL_LOG"\nexit 0\n')
    fake.chmod(0o755)
    return bindir, log


def _run(
    script_args: list[str], *, db: Path, bindir: Path, log: Path
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "AGENTS_DB": str(db),
        "TMUX_CALL_LOG": str(log),
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *script_args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_backfill_seeds_pane_label_for_live_panes_only(tmp_path: Path) -> None:
    db = tmp_path / "agents.db"
    _make_db(
        db,
        [
            ("live1", "auth-refactor", "%1", "idle"),
            ("live2", "docs-fix", "%3", "processing"),
            ("dead1", "gone", "%2", "stopped"),  # excluded: stopped
            ("noname", "", "%4", "idle"),  # excluded: empty name
            ("nopane", "has-name", "", "idle"),  # excluded: empty pane
        ],
    )
    bindir, log = _fake_tmux(tmp_path)

    proc = _run(["--backfill"], db=db, bindir=bindir, log=log)

    assert proc.returncode == 0, proc.stderr
    assert "2 pane(s)" in proc.stdout
    calls = log.read_text().splitlines() if log.exists() else []
    assert "set-option -p -t %1 @PANE_LABEL auth-refactor" in calls
    assert "set-option -p -t %3 @PANE_LABEL docs-fix" in calls
    # Excluded rows never touch tmux.
    assert not any("%2" in c for c in calls)
    assert not any("%4" in c for c in calls)
    assert not any("has-name" in c for c in calls)


def test_legacy_positional_invocation_is_a_noop(tmp_path: Path) -> None:
    db = tmp_path / "agents.db"
    _make_db(db, [("live1", "auth-refactor", "%1", "idle")])
    bindir, log = _fake_tmux(tmp_path)

    proc = _run(["%1", "some-title", "false"], db=db, bindir=bindir, log=log)

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not log.exists(), "legacy hot-path invocation must not shell out to tmux"
