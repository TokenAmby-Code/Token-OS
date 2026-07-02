"""Tests for the phone `mac` command shipped in Termux ~/.bashrc.

These exercise the real shell functions from mobile/termux-bashrc-template
instead of reimplementing the command in Python. External ssh is stubbed only at
the process boundary so assertions inspect the exact ssh argv and remote command
that the phone would run.
"""

import os
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


def _run_mac(tmp_path: Path, *, inside_tmux: bool = False) -> list[str]:
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
is_portable_monitor() {{ return 1; }}
ssh() {{ printf '%s\\0' "$@" > {str(ssh_log)!r}; return 0; }}
unset SSH_CONNECTION
{"export TMUX=/tmp/local-tmux" if inside_tmux else "unset TMUX"}
mac
"""
    env = os.environ.copy()
    env["HOME"] = str(home)
    subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, text=True)
    return [part.decode() for part in ssh_log.read_bytes().split(b"\0") if part]


def _remote_command(argv: list[str]) -> str:
    assert "mac" in argv, argv
    return argv[argv.index("mac") + 1]


def test_bare_phone_mac_allocates_tty_and_attaches_main_session(tmp_path: Path) -> None:
    argv = _run_mac(tmp_path)

    assert "-t" in argv[: argv.index("mac")], argv
    cmd = _remote_command(argv)
    assert "tmux attach -t main" in cmd
    assert "tmux attach -t phone" not in cmd
    assert "new-session -t main -s phone" not in cmd
    assert "tx" not in cmd


def test_tmux_pane_mac_also_drives_canonical_main_attach(tmp_path: Path) -> None:
    argv = _run_mac(tmp_path, inside_tmux=True)

    assert argv[:2] == ["-t", "mac"], argv
    cmd = _remote_command(argv)
    assert "tmux attach -t main" in cmd
    assert "tmux attach -t phone" not in cmd
    assert "new-session -t main -s phone" not in cmd
    assert "tx" not in cmd
