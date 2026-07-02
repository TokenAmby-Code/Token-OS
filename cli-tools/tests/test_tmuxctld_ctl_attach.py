"""Tests for the blessed human tmux attach surface."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CTL = ROOT / "tmuxctld" / "bin" / "tmuxctld-ctl"


def _run_ctl_attach(tmp_path: Path, *args: str) -> tuple[list[str], list[str]]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "tmux.argv0"
    raw_log = tmp_path / "tmux.raw0"
    tmux = fakebin / "tmux"
    tmux.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\0' \"$@\" >> {log!s}\n"
        f"printf '%s\\0' \"${{IMPERIUM_TMUX_RAW:-}}\" >> {raw_log!s}\n"
        'case "$1" in\n'
        "  has-session) exit 0 ;;\n"
        "  attach-session) exit 0 ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n"
    )
    tmux.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env.pop("IMPERIUM_TMUX_RAW", None)

    subprocess.run([str(CTL), "attach", *args], env=env, check=True, capture_output=True, text=True)
    argv = [part.decode() for part in log.read_bytes().split(b"\0") if part]
    raw = [part.decode() for part in raw_log.read_bytes().split(b"\0") if part]
    return argv, raw


def test_attach_defaults_to_main_and_execs_raw_attach_in_blessed_ctl(tmp_path: Path) -> None:
    argv, raw = _run_ctl_attach(tmp_path)

    assert raw == ["1", "1"]
    assert argv == [
        "has-session",
        "-t",
        "main",
        "attach-session",
        "-t",
        "main",
    ]


def test_attach_accepts_explicit_session_name(tmp_path: Path) -> None:
    argv, raw = _run_ctl_attach(tmp_path, "main")

    assert raw == ["1", "1"]
    assert argv[-3:] == ["attach-session", "-t", "main"]


def test_attach_rejects_option_like_session_names(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    tmux = fakebin / "tmux"
    tmux.write_text("#!/usr/bin/env bash\nexit 0\n")
    tmux.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"

    result = subprocess.run(
        [str(CTL), "attach", "-bad"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "invalid session" in result.stderr
