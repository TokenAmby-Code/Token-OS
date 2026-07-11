"""Tests for the blessed single-main tmux startup surface."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CTL = ROOT / "tmuxctld" / "bin" / "tmuxctld-ctl"


_HEALTHY_PANES = """main council:custodes claude /Volumes/Imperium/Imperium-ENV
main council:malcador claude /Volumes/Imperium/Imperium-ENV
main council:administratum claude /Volumes/Imperium/Imperium-ENV
main mechanicus:fabricator-general codex /Volumes/Imperium/Imperium-ENV
main reservists:token-os codex /Users/tokenclaw/runtimes/Token-OS/live
"""


_INCOMPLETE_PANES = """main council:custodes claude /Volumes/Imperium/Imperium-ENV
"""


def _run_ctl(
    tmp_path: Path,
    *args: str,
    has_session: bool = True,
    panes: str = _HEALTHY_PANES,
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str], list[str]]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "tmux.argv0"
    raw_log = tmp_path / "tmux.raw0"
    ping_log = tmp_path / "ping.argv0"
    state = tmp_path / "session.exists"
    panes_file = tmp_path / "panes.txt"
    panes_file.write_text(panes)
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
        f"  list-panes) cat {panes_file!s} ;;\n"
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
        'printf \'{"ok":true,"result":"ok"}\'\n'
    )
    ping.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["TMUXCTLD_CTL_NAS_WAIT_DISABLE"] = "1"
    env["TMUXCTLD_CTL_SKIP_DAEMON_HEALTH"] = "1"
    env["TMUXCTLD_PING_BIN"] = str(ping)
    env.pop("IMPERIUM_TMUX_RAW", None)

    proc = subprocess.run([str(CTL), *args], env=env, capture_output=True, text=True, timeout=10)
    argv = [part.decode() for part in log.read_bytes().split(b"\0") if part] if log.exists() else []
    raw = (
        [part.decode() for part in raw_log.read_bytes().split(b"\0") if part]
        if raw_log.exists()
        else []
    )
    ping_argv = (
        [part.decode() for part in ping_log.read_bytes().split(b"\0") if part]
        if ping_log.exists()
        else []
    )
    return proc, argv, raw, ping_argv


def test_attach_defaults_to_main_and_execs_raw_attach_in_blessed_ctl(tmp_path: Path) -> None:
    proc, argv, raw, ping_argv = _run_ctl(tmp_path, "attach")

    assert proc.returncode == 0, proc.stderr
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


def test_attach_accepts_explicit_main(tmp_path: Path) -> None:
    proc, argv, raw, _ping_argv = _run_ctl(tmp_path, "attach", "main")

    assert proc.returncode == 0, proc.stderr
    assert raw and all(value == "1" for value in raw)
    assert argv[-3:] == ["attach-session", "-t", "main"]


def test_attach_rejects_non_main_session(tmp_path: Path) -> None:
    proc, argv, _raw, ping_argv = _run_ctl(tmp_path, "attach", "sandbox")

    assert proc.returncode != 0
    assert "only the 'main' tmux session is supported" in proc.stderr
    assert argv == []
    assert ping_argv == []


def test_attach_rejects_option_like_session_names(tmp_path: Path) -> None:
    proc, argv, _raw, ping_argv = _run_ctl(tmp_path, "attach", "-bad")

    assert proc.returncode != 0
    assert "invalid session" in proc.stderr
    assert argv == []
    assert ping_argv == []


def test_attach_creates_missing_main_before_raw_attach(tmp_path: Path) -> None:
    proc, argv, raw, ping_argv = _run_ctl(tmp_path, "attach", has_session=False)

    assert proc.returncode == 0, proc.stderr
    assert raw[-1] == "1"
    assert ping_argv == ["POST", "/create", "session=main"]
    assert argv[-3:] == ["attach-session", "-t", "main"]


def test_workspace_rebuild_forces_restart_without_attach(tmp_path: Path) -> None:
    proc, argv, _raw, ping_argv = _run_ctl(tmp_path, "workspace", "--rebuild")

    assert proc.returncode == 0, proc.stderr
    assert ping_argv == ["POST", "/restart", "session=main", "dry_run=false"]
    assert argv[-3:] == ["has-session", "-t", "main"]
    assert "attach-session" not in argv


def test_incomplete_main_rebuilds_when_no_clients(tmp_path: Path) -> None:
    proc, _argv, _raw, ping_argv = _run_ctl(tmp_path, "workspace", panes=_INCOMPLETE_PANES)

    assert proc.returncode == 0, proc.stderr
    assert ping_argv == ["POST", "/restart", "session=main", "dry_run=false"]
