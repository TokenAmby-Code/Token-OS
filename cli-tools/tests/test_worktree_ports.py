from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKTREE_PORTS = ROOT / "cli-tools" / "lib" / "worktree-ports.sh"


def _run_bash(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env={"HOME": str(tmp_path), "WORKTREE_PORTS_NO_FLOCK": "1"},
        text=True,
        capture_output=True,
        timeout=20,
    )


def test_stop_port_process_kills_assigned_71xx_listener_and_keeps_registry(tmp_path: Path) -> None:
    wt = tmp_path / "worktrees" / "Token-OS" / "wt-feature"
    wt.mkdir(parents=True)
    reg_dir = tmp_path / ".local" / "state" / "imperium"
    reg_dir.mkdir(parents=True)
    reg = reg_dir / "worktree-ports.json"
    reg.write_text(json.dumps({str(wt): 7108}), encoding="utf-8")
    calls = tmp_path / "calls.log"
    worktree_ports_q = shlex.quote(str(WORKTREE_PORTS))
    calls_q = shlex.quote(str(calls))
    wt_q = shlex.quote(str(wt))

    result = _run_bash(
        f"""
        set -euo pipefail
        source {worktree_ports_q}
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
    assert json.loads(reg.read_text(encoding="utf-8")) == {str(wt): 7108}
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "lsof -tiTCP:7108 -sTCP:LISTEN",
        "kill -INT 123 456",
        "sleep 1",
        "kill -KILL 123 456",
    ]


def test_stop_port_process_refuses_7777_and_out_of_pool_ports(tmp_path: Path) -> None:
    live = tmp_path / "worktrees" / "Token-OS" / "wt-live"
    other = tmp_path / "worktrees" / "Token-OS" / "wt-other"
    live.mkdir(parents=True)
    other.mkdir(parents=True)
    reg_dir = tmp_path / ".local" / "state" / "imperium"
    reg_dir.mkdir(parents=True)
    reg = reg_dir / "worktree-ports.json"
    reg.write_text(json.dumps({str(live): 7777, str(other): 7200}), encoding="utf-8")
    calls = tmp_path / "calls.log"
    worktree_ports_q = shlex.quote(str(WORKTREE_PORTS))
    calls_q = shlex.quote(str(calls))
    live_q = shlex.quote(str(live))
    other_q = shlex.quote(str(other))

    result = _run_bash(
        f"""
        set -euo pipefail
        source {worktree_ports_q}
        lsof() {{ printf 'lsof %s\n' "$*" >> {calls_q}; printf '999\n'; }}
        kill() {{ printf 'kill %s\n' "$*" >> {calls_q}; }}
        stop_port_process {live_q}
        stop_port_process {other_q}
        """,
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(reg.read_text(encoding="utf-8")) == {str(live): 7777, str(other): 7200}
    assert not calls.exists(), "guarded ports must not call lsof/kill"
