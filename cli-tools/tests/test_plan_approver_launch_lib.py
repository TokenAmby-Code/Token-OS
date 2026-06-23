from __future__ import annotations

import os
import pathlib
import shlex
import stat
import subprocess
import time
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIB = ROOT / "lib" / "plan-approver-launch.sh"


def _write_exe(path: pathlib.Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _wait_for_lines(path: pathlib.Path, expected: int) -> None:
    # ~15s retry budget (was ~2.5s); widened for CPU contention under parallel runs.
    for _ in range(300):
        if path.exists() and len(path.read_text().strip().splitlines()) >= expected:
            return
        time.sleep(0.05)


def test_trigger_classes_map_to_timeout_and_always_no_state(tmp_path: pathlib.Path) -> None:
    approver = tmp_path / "approver"
    argv_log = tmp_path / "argv.log"
    launch_log = tmp_path / "launch.log"
    _write_exe(approver, f'printf "%s\\n" "$*" >> "{argv_log}"\n')

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    cases = {
        "precise_permission": "10",
        "early_prompt": "90",
        "post_tool": "30",
        "late_stop": "10",
    }
    for trigger in cases:
        command = " ".join(
            [
                "source",
                shlex.quote(str(LIB)) + ";",
                "plan_approver_launch",
                "--agent",
                "codex",
                "--trigger-class",
                shlex.quote(trigger),
                "--pane",
                shlex.quote("%42"),
                "--approver",
                shlex.quote(str(approver)),
                "--log-file",
                shlex.quote(str(launch_log)),
                "--reason",
                shlex.quote(trigger),
            ]
        )
        subprocess.run(
            ["bash", "-c", command],
            env=env,
            check=True,
            timeout=10,
        )
    _wait_for_lines(argv_log, expected=len(cases))
    lines = argv_log.read_text().strip().splitlines()
    assert Counter(lines) == Counter(
        f"--pane %42 --agent codex --timeout {timeout} --no-state" for timeout in cases.values()
    )
    log_text = launch_log.read_text()
    for trigger in cases:
        assert f"trigger={trigger}" in log_text
    assert "state_policy=no-state" in log_text


def test_resolve_pane_prefers_env_then_hook_json_then_dispatch_then_pid_walk(
    tmp_path: pathlib.Path,
) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _write_exe(fakebin / "agent-cmd", 'printf "%s" "%pid"\n')

    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["HOME"] = str(tmp_path)

    def resolve(extra_env: dict[str, str], hook: str = "{}") -> str:
        run_env = env.copy()
        run_env.pop("TMUX_PANE", None)
        run_env.pop("TOKEN_API_DISPATCH_RESOLVED_PANE", None)
        run_env.update(extra_env)
        command = " ".join(
            [
                "source",
                shlex.quote(str(LIB)) + ";",
                "plan_approver_resolve_pane",
                shlex.quote(""),
                shlex.quote(hook),
                shlex.quote(""),
            ]
        )
        out = subprocess.check_output(
            ["bash", "-c", command],
            env=run_env,
            text=True,
            timeout=10,
        )
        return out.strip()

    assert resolve({"TMUX_PANE": "%env"}, '{"env":{"TMUX_PANE":"%json"}}') == "%env"
    assert resolve({}, '{"env":{"TMUX_PANE":"%json"}}') == "%json"
    assert resolve({"TOKEN_API_DISPATCH_RESOLVED_PANE": "%dispatch"}) == "%dispatch"
    assert resolve({}) == "%pid"
