"""Tests for the phone `mac` command shipped in Termux ~/.bashrc.

These exercise the real shell functions from mobile/termux-bashrc-template
instead of reimplementing the command in Python. External ssh is stubbed only at
the process boundary so assertions inspect the exact ssh argv and remote command
that the phone would run.
"""

import os
import shlex
import subprocess
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "mobile" / "termux-bashrc-template"


def _extract_fn(name: str) -> str:
    """Slice a top-level shell function definition out of the template."""
    lines = TEMPLATE.read_text().splitlines()
    out, capturing = [], False
    for ln in lines:
        if ln.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            out.append(ln)
        if capturing and ln == "}":
            return "\n".join(out)
    raise AssertionError(f"function {name} not found in {TEMPLATE}")


EXPECTED_REMOTE_ATTACH = "zsh -il -c 'exec tmuxctld-ctl attach'"


def _run_mac(tmp_path: Path, *, inside_tmux: bool = False, portable: bool = False) -> list[str]:
    ssh_log = tmp_path / "ssh.argv0"
    home = tmp_path / "home"
    home.mkdir()
    funcs = "\n".join(
        _extract_fn(name)
        for name in (
            "_mac_tmux_attach_cmd",
            "_mac_insist_tmux_enabled",
            "_mac_set_insist_tmux",
            "_mac_reconnect_plan",
            "mac",
        )
    )
    script = f"""
set -eo pipefail
TERMUX_MAC_TMUX_CLIENT_TAG=pytest-client
MAC_INSIST_TMUX=true
MAC_INSIST_TMUX_FILE="$HOME/.mac-insist-tmux"
MAC_NOT_MAC_FILE="$HOME/.not-mac"
{funcs}
is_portable_monitor() {{ [[ "${{PYTEST_PORTABLE_MONITOR:-0}}" == "1" ]]; }}
ssh() {{ printf '%s\\0' "$@" > {shlex.quote(str(ssh_log))}; return 0; }}
unset SSH_CONNECTION
{"export TMUX=/tmp/local-tmux" if inside_tmux else "unset TMUX"}
mac
"""
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTEST_PORTABLE_MONITOR"] = "1" if portable else "0"
    subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, text=True)
    return [part.decode() for part in ssh_log.read_bytes().split(b"\0") if part]


def _remote_command(argv: list[str]) -> str:
    assert "mac" in argv, argv
    return argv[argv.index("mac") + 1]


def test_bare_phone_mac_allocates_tty_and_uses_blessed_attach(tmp_path: Path) -> None:
    argv = _run_mac(tmp_path)

    assert "-t" in argv[: argv.index("mac")], argv
    cmd = _remote_command(argv)
    assert cmd == EXPECTED_REMOTE_ATTACH
    assert "tmux attach" not in cmd
    assert "attach-session" not in cmd
    assert "IMPERIUM_TMUX_RAW" not in cmd
    assert "tx" not in cmd


def test_tmux_pane_mac_also_uses_blessed_attach(tmp_path: Path) -> None:
    argv = _run_mac(tmp_path, inside_tmux=True)

    assert argv == ["-t", "mac", EXPECTED_REMOTE_ATTACH], argv


def test_portable_monitor_path_uses_same_blessed_attach(tmp_path: Path) -> None:
    argv = _run_mac(tmp_path, portable=True)

    assert "-t" in argv[: argv.index("mac")], argv
    assert _remote_command(argv) == EXPECTED_REMOTE_ATTACH
