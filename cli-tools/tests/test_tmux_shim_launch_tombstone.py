"""Regression pins for tmux launch-command routing."""

from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMUX_SHIM = ROOT / "bin" / "tmux"


def _fake_real_tmux(tmp_path: pathlib.Path) -> pathlib.Path:
    fake = tmp_path / "real-tmux"
    fake.write_text('#!/usr/bin/env bash\nprintf \'%s\\0\' "$@" > "$TMUX_SHIM_TEST_LOG"\nexit 0\n')
    fake.chmod(0o755)
    return fake


def _fake_ctl(
    tmp_path: pathlib.Path, *, reject_non_main: bool = False
) -> tuple[pathlib.Path, pathlib.Path]:
    ctl_log = tmp_path / "ctl.argv0"
    fake_ctl = tmp_path / "tmuxctld-ctl"
    if reject_non_main:
        fake_ctl.write_text(
            f"#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" > {ctl_log!s}\n"
            'if [[ "${2:-main}" != "main" ]]; then echo "only main" >&2; exit 64; fi\n'
            "exit 0\n"
        )
    else:
        fake_ctl.write_text(f"#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" > {ctl_log!s}\nexit 0\n")
    fake_ctl.chmod(0o755)
    return fake_ctl, ctl_log


def _run_launch(
    tmp_path: pathlib.Path, *argv: str, reject_non_main: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake_ctl, ctl_log = _fake_ctl(tmp_path, reject_non_main=reject_non_main)
    env = os.environ.copy()
    env["IMPERIUM_TMUX_BIN"] = str(_fake_real_tmux(tmp_path))
    env["TMUXCTLD_CTL_BIN"] = str(fake_ctl)
    env.pop("IMPERIUM_TMUX_RAW", None)

    proc = subprocess.run(
        [str(TMUX_SHIM), *argv],
        env=env,
        text=True,
        capture_output=True,
    )
    ctl_argv = [part.decode() for part in ctl_log.read_bytes().split(b"\0") if part]
    return proc, ctl_argv


def test_raw_human_attach_routes_to_tmuxctld_attach_main(tmp_path: pathlib.Path) -> None:
    proc, argv = _run_launch(tmp_path, "attach", "-t", "main")

    assert proc.returncode == 0, proc.stderr
    assert argv == ["attach", "main"]


def test_raw_human_new_routes_to_tmuxctld_attach_main(tmp_path: pathlib.Path) -> None:
    proc, argv = _run_launch(tmp_path, "new")

    assert proc.returncode == 0, proc.stderr
    assert argv == ["attach", "main"]


def test_non_main_launch_target_fails_through_control_path(tmp_path: pathlib.Path) -> None:
    proc, argv = _run_launch(tmp_path, "new-session", "-s", "sandbox", reject_non_main=True)

    assert proc.returncode == 64
    assert "only main" in proc.stderr
    assert argv == ["attach", "sandbox"]


def test_raw_env_remains_internal_escape_hatch(tmp_path: pathlib.Path) -> None:
    log = tmp_path / "argv0"
    env = os.environ.copy()
    env["IMPERIUM_TMUX_BIN"] = str(_fake_real_tmux(tmp_path))
    env["IMPERIUM_TMUX_RAW"] = "1"
    env["TMUX_SHIM_TEST_LOG"] = str(log)

    proc = subprocess.run(
        [str(TMUX_SHIM), "attach-session", "-t", "main"],
        env=env,
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 0, proc.stderr
    argv = [part.decode() for part in log.read_bytes().split(b"\0") if part]
    assert argv == ["attach-session", "-t", "main"]
