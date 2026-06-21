"""The session-destructive `tx restart` is HUMAN-ONLY and must HARD-refuse when
invoked from an agent/automation harness (Claude Code, Codex, the subagent CLI).

The pre-existing TTY guard only fails closed for non-TTY callers, but an agent's
shell running inside a tmux pane HOLDS a TTY — so a TTY check alone cannot tell a
human from an agent. Agent-invoked `tx restart` is exactly what wiped the live
fleet. These tests assert the env-based agent guard fires FIRST (its message, not
the TTY guard's, is what the agent sees) and that the destructive rebuild is never
reached. The graceful CD reload (`token-restart --sync`, a separate binary) is
covered separately in test_token_restart_smart_deploy.py and must stay available
to automation — it is never gated here.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
TX = ROOT / "bin" / "tx"

# Every env marker the guard treats as an agent/automation context.
AGENT_MARKERS = [
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "TOKEN_API_SUBAGENT",
    "CODEX_PROFILE",
    "CODEX_HEADLESS",
    "CODEX_BRIDGE_ID",
    "TOKEN_API_CODEX_BRIDGE_ID",
    "TOKEN_API_CODEX_PROFILE",
]


def _base_env(tmp_path: pathlib.Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """A controlled env with ALL agent markers stripped (the test runner itself
    may run under Claude Code, which sets CLAUDECODE). A fake tmux on PATH keeps
    any incidental tmux call inert. Callers re-add exactly the marker they test."""
    env = {k: v for k, v in os.environ.items() if k not in AGENT_MARKERS}
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    fake_tmux = stub / "tmux"
    fake_tmux.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_tmux.chmod(0o755)
    env["PATH"] = f"{stub}:{env['PATH']}"
    env["IMPERIUM_MACHINE"] = "test"
    env["TX_INVOCATION_LOG"] = str(tmp_path / "tx-invocations.log")
    env["TX_RESTART_WATCHDOG_LOG"] = str(tmp_path / "watchdog.log")
    if extra:
        env.update(extra)
    return env


def _run_restart(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # --force would skip the typed confirmation for a real human; it must NOT let
    # an agent through the agent guard.
    return subprocess.run(
        [str(TX), "restart", "--force"],
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )


def _assert_refused_as_agent(proc: subprocess.CompletedProcess[str]) -> None:
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "agent/automation context" in out, out
    # The destructive rebuild is never announced/reached.
    assert "Restarting session" not in out, out


def test_claude_code_context_refuses_restart(tmp_path: pathlib.Path) -> None:
    env = _base_env(tmp_path, {"CLAUDECODE": "1"})
    proc = _run_restart(env)
    _assert_refused_as_agent(proc)
    inv = pathlib.Path(env["TX_INVOCATION_LOG"])
    assert inv.exists() and "refused-agent-context" in inv.read_text()


def test_codex_context_refuses_restart(tmp_path: pathlib.Path) -> None:
    env = _base_env(tmp_path, {"CODEX_PROFILE": "mechanicus"})
    _assert_refused_as_agent(_run_restart(env))


def test_subagent_context_refuses_restart(tmp_path: pathlib.Path) -> None:
    env = _base_env(tmp_path, {"TOKEN_API_SUBAGENT": "subagent:claude"})
    _assert_refused_as_agent(_run_restart(env))


def test_clean_human_env_is_not_caught_by_agent_guard(tmp_path: pathlib.Path) -> None:
    """Separation proof: with NO agent markers, the agent guard does NOT fire — the
    only reason this non-TTY subprocess is refused is the pre-existing TTY guard (a
    real human terminal satisfies it). This guarantees the new block does not
    over-refuse humans and is cleanly distinct from the graceful path."""
    proc = _run_restart(_base_env(tmp_path))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "agent/automation context" not in out, out
    assert "interactive terminal" in out, out
