"""Tests for the phone `mac` command shipped in Termux ~/.bashrc.

These exercise the real shell functions from mobile/termux-bashrc-template
instead of reimplementing the command in Python. External ssh is stubbed only at
the process boundary so assertions inspect the exact ssh argv and remote command
that the phone would run.
"""

import os
import shlex
import subprocess
import time
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


def _mac_script(
    tmp_path: Path,
    *,
    inside_tmux: bool = False,
    portable: bool = False,
    ssh_failures: int = 0,
    disable_gate_on_first_failure: bool = False,
) -> tuple[str, Path, Path, dict[str, str]]:
    ssh_log = tmp_path / "ssh.argv0"
    ssh_count = tmp_path / "ssh.count"
    home = tmp_path / "home"
    home.mkdir()
    funcs = "\n".join(
        _extract_fn(name)
        for name in (
            "_mac_tmux_attach_cmd",
            "_mac_insist_tmux_enabled",
            "_mac_set_insist_tmux",
            "_mac_reconnect_enabled",
            "_mac_wait_for_reconnect_enabled",
            "mac-reconnect-on",
            "mac-reconnect-off",
            "_mac_reconnect_plan",
            "mac",
        )
    )
    script = f"""
set -o pipefail
TERMUX_MAC_TMUX_CLIENT_TAG=pytest-client
MAC_INSIST_TMUX=true
MAC_INSIST_TMUX_FILE="$HOME/.mac-insist-tmux"
MAC_NOT_MAC_FILE="$HOME/.not-mac"
MAC_RECONNECT_FILE="$HOME/.mac-reconnect-enabled"
MAC_RECONNECT_FIFO="$HOME/.mac-reconnect-wake"
{funcs}
is_portable_monitor() {{ [[ "${{PYTEST_PORTABLE_MONITOR:-0}}" == "1" ]]; }}
ssh() {{
    local n
    n=$(cat {shlex.quote(str(ssh_count))} 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" > {shlex.quote(str(ssh_count))}
    printf '%s\\0' "$@" >> {shlex.quote(str(ssh_log))}
    if (( n <= {ssh_failures} )); then
        if (( n == 1 && {1 if disable_gate_on_first_failure else 0} == 1 )); then
            echo false > "$MAC_RECONNECT_FILE"
        fi
        return 255
    fi
    return 0
}}
unset SSH_CONNECTION
{"export TMUX=/tmp/local-tmux" if inside_tmux else "unset TMUX"}
mac
"""
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTEST_PORTABLE_MONITOR"] = "1" if portable else "0"
    return script, ssh_log, ssh_count, env


def _run_mac(tmp_path: Path, **kwargs) -> tuple[list[str], subprocess.CompletedProcess[str]]:
    script, ssh_log, _, env = _mac_script(tmp_path, **kwargs)
    result = subprocess.run(
        ["bash", "-c", script], env=env, check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    argv = [part.decode() for part in ssh_log.read_bytes().split(b"\0") if part]
    return argv, result


def _remote_command(argv: list[str]) -> str:
    assert "mac" in argv, argv
    return argv[argv.index("mac") + 1]


def test_bare_phone_mac_allocates_tty_and_uses_blessed_attach(tmp_path: Path) -> None:
    argv, _ = _run_mac(tmp_path)

    assert "-t" in argv[: argv.index("mac")], argv
    cmd = _remote_command(argv)
    assert cmd == EXPECTED_REMOTE_ATTACH
    assert "tmux attach" not in cmd
    assert "attach-session" not in cmd
    assert "IMPERIUM_TMUX_RAW" not in cmd
    assert "tx" not in cmd


def test_tmux_pane_mac_also_uses_blessed_attach(tmp_path: Path) -> None:
    argv, _ = _run_mac(tmp_path, inside_tmux=True)

    assert argv == ["-t", "mac", EXPECTED_REMOTE_ATTACH], argv


def test_portable_monitor_path_uses_same_blessed_attach(tmp_path: Path) -> None:
    argv, _ = _run_mac(tmp_path, portable=True)

    assert "-t" in argv[: argv.index("mac")], argv
    assert _remote_command(argv) == EXPECTED_REMOTE_ATTACH


def test_bare_phone_mac_ssh_failure_reconnects_aggressively(tmp_path: Path) -> None:
    argv, result = _run_mac(tmp_path, ssh_failures=1)

    assert result.stderr == ""
    assert result.stdout.count("Connection dropped") == 0
    assert argv.count("mac") == 2, argv
    assert _remote_command(argv) == EXPECTED_REMOTE_ATTACH


def test_bare_phone_mac_pauses_without_polling_when_reconnect_gate_false(tmp_path: Path) -> None:
    script, ssh_log, ssh_count, env = _mac_script(tmp_path, ssh_failures=1)
    home = Path(env["HOME"])
    (home / ".mac-reconnect-enabled").write_text("false\n")

    proc = subprocess.Popen(
        ["bash", "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        for _ in range(50):
            if (home / ".mac-reconnect-wake").exists():
                break
            time.sleep(0.02)
        assert proc.poll() is None
        assert not ssh_count.exists()

        (home / ".mac-reconnect-enabled").write_text("true\n")
        with (home / ".mac-reconnect-wake").open("w") as fifo:
            fifo.write("wake\n")

        stdout, stderr = proc.communicate(timeout=3)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert stderr == ""
    assert "Mac reconnect paused; waiting for Termux reopen..." in stdout
    argv = [part.decode() for part in ssh_log.read_bytes().split(b"\0") if part]
    assert argv.count("mac") == 2, argv


def test_bare_phone_mac_drop_waits_when_reconnect_gate_turns_false(tmp_path: Path) -> None:
    script, ssh_log, ssh_count, env = _mac_script(
        tmp_path,
        ssh_failures=1,
        disable_gate_on_first_failure=True,
    )
    home = Path(env["HOME"])

    proc = subprocess.Popen(
        ["bash", "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        for _ in range(50):
            if (home / ".mac-reconnect-wake").exists():
                break
            time.sleep(0.02)
        assert proc.poll() is None
        assert ssh_count.read_text().strip() == "1"

        (home / ".mac-reconnect-enabled").write_text("true\n")
        with (home / ".mac-reconnect-wake").open("w") as fifo:
            fifo.write("wake\n")

        stdout, stderr = proc.communicate(timeout=3)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert stderr == ""
    assert "Mac reconnect paused; waiting for Termux reopen..." in stdout
    argv = [part.decode() for part in ssh_log.read_bytes().split(b"\0") if part]
    assert argv.count("mac") == 2, argv
