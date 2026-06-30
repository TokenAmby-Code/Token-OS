from __future__ import annotations

import os
import shlex
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKTREE_PORTS = ROOT / "cli-tools" / "lib" / "worktree-ports.sh"
WORKTREE_PORTS_BIN = ROOT / "cli-tools" / "bin" / "worktree-ports"


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE instances (
                id TEXT PRIMARY KEY,
                working_dir TEXT,
                status TEXT,
                stopped_at TEXT,
                archived_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_instance(
    db: Path,
    *,
    instance_id: str,
    working_dir: Path,
    status: str,
    stopped_at: str | None = None,
    archived_at: str | None = None,
) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO instances (id, working_dir, status, stopped_at, archived_at) VALUES (?, ?, ?, ?, ?)",
            (instance_id, str(working_dir), status, stopped_at, archived_at),
        )
        conn.commit()
    finally:
        conn.close()


def _write_env(wt: Path, port: int) -> None:
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".worktree.env").write_text(f"PORT={port}\n", encoding="utf-8")


def _run_bash(
    script: str, tmp_path: Path, *, db: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "WORKTREE_PORTS_NO_FLOCK": "1",
    }
    if db is not None:
        env["TOKEN_API_AGENTS_DB"] = str(db)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )


def test_assignment_ignores_stale_full_legacy_registry(tmp_path: Path) -> None:
    reg_dir = tmp_path / ".local" / "state" / "imperium"
    reg_dir.mkdir(parents=True)
    stale = {f"/stale/wt-{port}": port for port in range(7100, 7200)}
    (reg_dir / "worktree-ports.json").write_text(str(stale).replace("'", '"'), encoding="utf-8")
    db = tmp_path / "agents.db"
    _make_db(db)
    wt = tmp_path / "worktrees" / "Token-OS" / "wt-new"
    wt.mkdir(parents=True)

    result = _run_bash(
        f"""
        set -euo pipefail
        source {shlex.quote(str(WORKTREE_PORTS))}
        assign_port {shlex.quote(str(wt))}
        """,
        tmp_path,
        db=db,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "7100"


def test_stopped_instance_owner_drops_out_of_allocation(tmp_path: Path) -> None:
    db = tmp_path / "agents.db"
    _make_db(db)
    stopped = tmp_path / "worktrees" / "Token-OS" / "wt-stopped"
    live = tmp_path / "worktrees" / "Token-OS" / "wt-live"
    new = tmp_path / "worktrees" / "Token-OS" / "wt-new"
    _write_env(stopped, 7100)
    _write_env(live, 7101)
    new.mkdir(parents=True)
    _insert_instance(
        db,
        instance_id="stopped-1",
        working_dir=stopped,
        status="stopped",
        stopped_at="2026-06-29T10:00:00",
    )
    _insert_instance(db, instance_id="live-1", working_dir=live, status="working")

    result = _run_bash(
        f"""
        set -euo pipefail
        source {shlex.quote(str(WORKTREE_PORTS))}
        assign_port {shlex.quote(str(new))}
        """,
        tmp_path,
        db=db,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "7100"


def test_concurrent_assignments_get_distinct_short_lived_leases(tmp_path: Path) -> None:
    db = tmp_path / "agents.db"
    _make_db(db)
    wt1 = tmp_path / "worktrees" / "Token-OS" / "wt-one"
    wt2 = tmp_path / "worktrees" / "Token-OS" / "wt-two"
    wt1.mkdir(parents=True)
    wt2.mkdir(parents=True)
    out1 = tmp_path / "one.out"
    out2 = tmp_path / "two.out"

    result = _run_bash(
        f"""
        set -euo pipefail
        source {shlex.quote(str(WORKTREE_PORTS))}
        assign_port {shlex.quote(str(wt1))} > {shlex.quote(str(out1))} &
        p1=$!
        assign_port {shlex.quote(str(wt2))} > {shlex.quote(str(out2))} &
        p2=$!
        wait "$p1"
        wait "$p2"
        """,
        tmp_path,
        db=db,
    )

    assert result.returncode == 0, result.stderr
    ports = {out1.read_text(encoding="utf-8").strip(), out2.read_text(encoding="utf-8").strip()}
    assert ports == {"7100", "7101"}


def test_stop_port_process_kills_assigned_71xx_listener_from_worktree_env(tmp_path: Path) -> None:
    wt = tmp_path / "worktrees" / "Token-OS" / "wt-feature"
    _write_env(wt, 7108)
    calls = tmp_path / "calls.log"
    calls_q = shlex.quote(str(calls))
    wt_q = shlex.quote(str(wt))

    result = _run_bash(
        f"""
        set -euo pipefail
        source {shlex.quote(str(WORKTREE_PORTS))}
        lsof() {{
            printf 'lsof %s\n' "$*" >> {calls_q}
            [[ "$*" == *'-tiTCP:7108'* ]] && printf '123\n456\n'
        }}
        kill() {{ printf 'kill %s\n' "$*" >> {calls_q}; }}
        sleep() {{ printf 'sleep %s\n' "$*" >> {calls_q}; }}
        stop_port_process {wt_q}
        """,
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "lsof -tiTCP:7108 -sTCP:LISTEN",
        "kill -INT 123 456",
        "sleep 1",
        "kill -KILL 123 456",
    ]


def test_stop_port_process_refuses_7777_and_out_of_pool_ports(tmp_path: Path) -> None:
    live = tmp_path / "worktrees" / "Token-OS" / "wt-live"
    other = tmp_path / "worktrees" / "Token-OS" / "wt-other"
    _write_env(live, 7777)
    _write_env(other, 7200)
    calls = tmp_path / "calls.log"
    calls_q = shlex.quote(str(calls))
    live_q = shlex.quote(str(live))
    other_q = shlex.quote(str(other))

    result = _run_bash(
        f"""
        set -euo pipefail
        source {shlex.quote(str(WORKTREE_PORTS))}
        lsof() {{ printf 'lsof %s\n' "$*" >> {calls_q}; printf '999\n'; }}
        kill() {{ printf 'kill %s\n' "$*" >> {calls_q}; }}
        stop_port_process {live_q}
        stop_port_process {other_q}
        """,
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert not calls.exists(), "guarded ports must not call lsof/kill"


def test_diagnostic_lists_live_owners_and_free_candidates(tmp_path: Path) -> None:
    db = tmp_path / "agents.db"
    _make_db(db)
    live = tmp_path / "worktrees" / "Token-OS" / "wt-live"
    _write_env(live, 7100)
    _insert_instance(db, instance_id="live-1", working_dir=live, status="idle")

    result = _run_bash(
        f"""
        set -euo pipefail
        {shlex.quote(str(WORKTREE_PORTS_BIN))} list
        """,
        tmp_path,
        db=db,
    )

    assert result.returncode == 0, result.stderr
    assert "PORT\tOWNER\tWORKTREE\tSTATUS" in result.stdout
    assert f"7100\tinstance:live-1\t{live}\tidle" in result.stdout
    assert "FREE\t7101" in result.stdout
