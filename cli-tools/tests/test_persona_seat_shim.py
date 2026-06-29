"""Guard tests for the thin persona-seat shim (cli-tools/scripts/persona-seat.sh).

The shim is the daemon-native replacement for shelling `dispatch`. Its load-bearing
properties — it `exec`s the engine (so agent-exit == pane-died, fast reap), it does
NOT run the blocking close-POST (`token_wrapper_end`), it reuses the ONE staple
source, and it fires the audit ping async — are asserted here against the file so a
future edit cannot silently reintroduce the wrapper-lingering reap stall.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHIM = ROOT / "scripts" / "persona-seat.sh"


def test_shim_exists_and_is_executable() -> None:
    assert SHIM.is_file(), f"missing shim: {SHIM}"
    assert os.access(SHIM, os.X_OK), "persona-seat.sh must be executable (tracked 0755)"


def test_shim_is_valid_bash() -> None:
    proc = subprocess.run(["bash", "-n", str(SHIM)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def _code_lines() -> str:
    """Shim body with whole-line comments stripped (so doc prose about what the
    shim does NOT do is not mistaken for the code doing it)."""
    return "\n".join(
        line
        for line in SHIM.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_shim_execs_the_engine_and_never_runs_the_blocking_close() -> None:
    code = _code_lines()
    # It hands the pane to the engine via exec (the reap-stall fix).
    assert 'exec "$ENGINE_BIN"' in code
    # It must NOT run the heavy wrapper's blocking close-POST or its cleanup trap —
    # those are exactly what made the pane reap slow.
    assert "token_wrapper_end" not in code
    assert "token_wrapper_cleanup_pane" not in code
    assert "trap " not in code


def test_shim_reuses_the_single_staple_source() -> None:
    body = SHIM.read_text(encoding="utf-8")
    assert "agent-wrapper-common.sh" in body
    assert "token_wrapper_compose_system_text" in body


def test_shim_audit_ping_is_async_fire_and_forget() -> None:
    body = SHIM.read_text(encoding="utf-8")
    assert "persona_seat_audit_ping & disown" in body
    # No retry belt on the hot path (the shared post-hook uses --retry-connrefused).
    assert "--retry-connrefused" not in body


def test_shim_bypasses_the_front_door_wrapper() -> None:
    body = SHIM.read_text(encoding="utf-8")
    assert "TOKEN_API_AGENT_WRAPPER_BYPASS=1" in body
    # Prefers the real engine binary, never re-enters a wrapper shim.
    assert ".token-os-real" in body


def test_shim_stamps_runtime_options_before_exec() -> None:
    body = SHIM.read_text(encoding="utf-8")
    assert "tmux-runtime-cleanup.sh" in body
    assert "tmux_runtime_stamp_wrapper" in body
    assert body.index("tmux_runtime_stamp_wrapper") < body.index('exec "$ENGINE_BIN"')
