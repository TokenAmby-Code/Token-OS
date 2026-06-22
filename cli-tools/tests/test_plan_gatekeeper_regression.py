from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "claude-config" / "hooks" / "plan-gatekeeper.sh"


def test_plan_gatekeeper_no_reject_once_bounce_state_machine():
    text = SCRIPT.read_text()
    assert "claude-plan-bounced" not in text
    assert 'behavior":"deny' not in text
    assert "plan_approver_launch" in text


def _write_stub(path: pathlib.Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_gatekeeper(
    tmp_path: pathlib.Path, *, resolve_pane: str, agent: str = "claude"
) -> pathlib.Path:
    """Run the hook with $TMUX_PANE stripped and stubbed token-os bins.

    Returns the marker file path the approver stub writes its argv to (it may or
    may not exist depending on whether the approver was launched).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "approver-args"

    # claude-cmd --self --resolve-only is the PID-walk pane recovery; emit the
    # configured pane (empty string => recovery fails).
    _write_stub(bindir / "claude-cmd", f'printf "%s" "{resolve_pane}"\n')
    _write_stub(bindir / "agent-cmd", f'printf "%s" "{resolve_pane}"\n')
    _write_stub(bindir / "tmuxctl", f'printf "%s" "{agent}"\n')
    # The approver records the argv it was invoked with so the test can assert
    # it ran against the recovered pane and explicit harness.
    _write_stub(
        bindir / "tmux-plan-approve-clear",
        f'printf "%s " "$@" > "{marker}"\n',
    )

    env = dict(os.environ)
    env.pop("TMUX_PANE", None)  # mimic Claude Code stripping the env var
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["HOME"] = str(tmp_path)  # keep the hook's log under the temp dir

    subprocess.run(
        ["bash", str(SCRIPT)],
        input='{"session_id":"recovery-test"}',
        env=env,
        text=True,
        check=True,
        timeout=20,
    )
    # The approver is launched in a disowned background subshell; poll briefly.
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.1)
    return marker


def test_plan_gatekeeper_recovers_pane_when_tmux_pane_stripped(
    tmp_path: pathlib.Path,
) -> None:
    # Claude Code strips $TMUX_PANE; the hook must recover the pane via the
    # PID walk and STILL launch the clear-context approver against it.
    marker = _run_gatekeeper(tmp_path, resolve_pane="%777")
    assert marker.exists(), "approver was not launched after pane recovery"
    argv = marker.read_text()
    assert "--pane %777" in argv
    assert "--agent claude" in argv
    assert "--agent auto" not in argv
    assert "--no-state" in argv


def test_plan_gatekeeper_uses_claude_agent_for_precise_permission(tmp_path: pathlib.Path) -> None:
    marker = _run_gatekeeper(tmp_path, resolve_pane="%778", agent="codex")
    assert marker.exists(), "approver was not launched for resolved pane"
    argv = marker.read_text()
    assert "--pane %778" in argv
    assert "--agent claude" in argv
    assert "--agent codex" not in argv


def test_plan_gatekeeper_yields_when_recovery_also_fails(
    tmp_path: pathlib.Path,
) -> None:
    # When neither $TMUX_PANE nor the PID walk yields a pane, the hook yields
    # without launching the approver (no bogus write to a stale/empty pane).
    marker = _run_gatekeeper(tmp_path, resolve_pane="")
    assert not marker.exists(), "approver must not run without a resolved pane"
