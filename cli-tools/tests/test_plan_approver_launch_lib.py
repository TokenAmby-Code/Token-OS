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


def _safe_key_name(value: str) -> str:
    command = " ".join(
        [
            "source",
            shlex.quote(str(LIB)) + ";",
            "plan_approver_safe_key_name",
            shlex.quote(value),
        ]
    )
    return subprocess.check_output(["bash", "-c", command], text=True, timeout=10).strip()


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TOKEN_API_SESSION_ID", None)
    env.pop("HOOK_INPUT", None)
    return env


def test_trigger_classes_map_to_timeout_and_always_no_state(tmp_path: pathlib.Path) -> None:
    approver = tmp_path / "approver"
    argv_log = tmp_path / "argv.log"
    launch_log = tmp_path / "launch.log"
    _write_exe(approver, f'printf "%s\\n" "$*" >> "{argv_log}"\n')

    env = _clean_env()
    env["HOME"] = str(tmp_path)
    cases = {
        "precise_permission": "10",
        "early_prompt": "300",
        "post_tool": "120",
        "late_stop": "30",
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
        f"--agent codex --timeout {timeout} --no-state --pane %42" for timeout in cases.values()
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

    env = _clean_env()
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


def test_launch_preserves_public_pane_target_and_lease(tmp_path: pathlib.Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    approver = tmp_path / "approver"
    argv_log = tmp_path / "argv.log"
    launch_log = tmp_path / "launch.log"
    _write_exe(approver, f'printf "%s\\n" "$*" >> "{argv_log}"\n')
    _write_exe(
        fakebin / "tmux",
        'if [[ "$1" == "display-message" ]]; then printf "%%123\\n"; exit 0; fi\nexit 1\n',
    )

    env = _clean_env()
    env["HOME"] = str(tmp_path)
    env["TMPDIR"] = str(tmp_path)
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    command = " ".join(
        [
            "source",
            shlex.quote(str(LIB)) + ";",
            "plan_approver_launch",
            "--agent codex --trigger-class post_tool --pane",
            shlex.quote("session:1.2"),
            "--approver",
            shlex.quote(str(approver)),
            "--log-file",
            shlex.quote(str(launch_log)),
        ]
    )
    subprocess.run(["bash", "-c", command], env=env, check=True, timeout=10)
    _wait_for_lines(argv_log, expected=1)
    assert (
        argv_log.read_text().strip() == "--agent codex --timeout 120 --no-state --pane session:1.2"
    )
    log_text = launch_log.read_text()
    assert "pane=session:1.2" in log_text
    assert "pane=%123" not in log_text
    lease_name = f"{_safe_key_name('session:1.2')}.deadline"
    assert (tmp_path / f"tmux-plan-approve-clear-{os.getuid()}" / lease_name).exists()


def test_refresh_lease_is_monotonic_for_shorter_later_timeout(tmp_path: pathlib.Path) -> None:
    env = _clean_env()
    env["TMPDIR"] = str(tmp_path)
    command = " ".join(
        [
            "source",
            shlex.quote(str(LIB)) + ";",
            "plan_approver_refresh_lease api-instance 300;",
            'root="$TMPDIR/tmux-plan-approve-clear-${UID}";',
            'first=$(cat "$root/api-instance.deadline");',
            "plan_approver_refresh_lease api-instance 30;",
            'second=$(cat "$root/api-instance.deadline");',
            'printf \'%s %s\' "$first" "$second"',
        ]
    )
    out = subprocess.check_output(["bash", "-c", command], env=env, text=True, timeout=10)
    first, second = [int(part) for part in out.split()]
    assert second == first


def test_resolve_instance_id_prefers_hook_payload_over_ambient_env(tmp_path: pathlib.Path) -> None:
    env = _clean_env()
    env["TOKEN_API_SESSION_ID"] = "ambient-stale"
    command = " ".join(
        [
            "source",
            shlex.quote(str(LIB)) + ";",
            "plan_approver_resolve_instance_id",
            shlex.quote('{"env":{"TOKEN_API_SESSION_ID":"hook-current"}}'),
        ]
    )
    out = subprocess.check_output(["bash", "-c", command], env=env, text=True, timeout=10)
    assert out.strip() == "hook-current"
