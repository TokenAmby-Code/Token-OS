"""Tests for the blessed human tmux attach surface."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CTL = ROOT / "tmuxctld" / "bin" / "tmuxctld-ctl"


def _run_ctl_attach(
    tmp_path: Path, *args: str, has_session: bool = True
) -> tuple[list[str], list[str], list[str]]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "tmux.argv0"
    raw_log = tmp_path / "tmux.raw0"
    ping_log = tmp_path / "ping.argv0"
    state = tmp_path / "session.exists"
    if has_session:
        state.write_text("1")
    tmux = fakebin / "tmux"
    tmux.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\0' \"$@\" >> {log!s}\n"
        f"printf '%s\\0' \"${{IMPERIUM_TMUX_RAW:-}}\" >> {raw_log!s}\n"
        'case "$1" in\n'
        f"  has-session) [[ -f {state!s} ]] ;;\n"
        "  attach-session) exit 0 ;;\n"
        "  list-panes)\n"
        "    cat <<'PANES'\n"
        "main council:custodes claude /Volumes/Imperium/Imperium-ENV\n"
        "main council:malcador claude /Volumes/Imperium/Imperium-ENV\n"
        "main council:administratum claude /Volumes/Imperium/Imperium-ENV\n"
        "main mechanicus:fabricator-general codex /Volumes/Imperium/Imperium-ENV\n"
        "main reservists:token-os codex /Users/tokenclaw/runtimes/Token-OS/live\n"
        "PANES\n"
        "    ;;\n"
        "  list-clients) exit 1 ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n"
    )
    tmux.chmod(0o755)
    ping = fakebin / "tmuxctld-ping"
    ping.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\0' \"$@\" >> {ping_log!s}\n"
        f"touch {state!s}\n"
        'printf \'{"ok":true,"result":"created"}\'\n'
    )
    ping.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["TMUXCTLD_CTL_NAS_WAIT_DISABLE"] = "1"
    env["TMUXCTLD_CTL_SKIP_DAEMON_HEALTH"] = "1"
    env["TMUXCTLD_PING_BIN"] = str(ping)
    env.pop("IMPERIUM_TMUX_RAW", None)

    subprocess.run([str(CTL), "attach", *args], env=env, check=True, capture_output=True, text=True)
    argv = [part.decode() for part in log.read_bytes().split(b"\0") if part]
    raw = [part.decode() for part in raw_log.read_bytes().split(b"\0") if part]
    ping_argv = (
        [part.decode() for part in ping_log.read_bytes().split(b"\0") if part]
        if ping_log.exists()
        else []
    )
    return argv, raw, ping_argv


def test_attach_defaults_to_main_and_execs_raw_attach_in_blessed_ctl(tmp_path: Path) -> None:
    argv, raw, ping_argv = _run_ctl_attach(tmp_path)

    assert raw and all(value == "1" for value in raw)
    assert ping_argv == []
    assert argv == [
        "has-session",
        "-t",
        "main",
        "has-session",
        "-t",
        "main",
        "list-panes",
        "-a",
        "-F",
        "#{session_name} #{@PANE_ID} #{pane_current_command} #{pane_current_path}",
        "has-session",
        "-t",
        "main",
        "attach-session",
        "-t",
        "main",
    ]


def test_attach_accepts_explicit_session_name(tmp_path: Path) -> None:
    argv, raw, _ping_argv = _run_ctl_attach(tmp_path, "main")

    assert raw and all(value == "1" for value in raw)
    assert argv[-3:] == ["attach-session", "-t", "main"]


def test_attach_creates_missing_workspace_before_raw_attach(tmp_path: Path) -> None:
    argv, raw, ping_argv = _run_ctl_attach(tmp_path, has_session=False)

    assert raw[-1] == "1"
    assert ping_argv == ["POST", "/create", "session=main"]
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
