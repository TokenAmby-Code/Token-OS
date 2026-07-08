"""Tests for the blessed human tmux attach surface."""

import os
import pty
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CTL = ROOT / "tmuxctld" / "bin" / "tmuxctld-ctl"


def _run_on_pty(
    argv: list[str], *, env: dict[str, str], timeout: float = 5.0
) -> subprocess.CompletedProcess[str]:
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            text=False,
            close_fds=True,
        )
        os.close(slave)
        slave = -1
        output = bytearray()
        deadline = time.time() + timeout
        while proc.poll() is None and time.time() < deadline:
            try:
                output.extend(os.read(master, 4096))
            except OSError:
                break
            time.sleep(0.01)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
            raise AssertionError(f"process timed out: {argv!r}")
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        return subprocess.CompletedProcess(
            argv, proc.returncode, output.decode(errors="replace"), ""
        )
    finally:
        if slave != -1:
            os.close(slave)
        os.close(master)


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

    assert raw == ["1", "1", "1"]
    assert argv == [
        "has-session",
        "-t",
        "main",
        "has-session",
        "-t",
        "main",
        "attach-session",
        "-t",
        "main",
    ]


def test_attach_accepts_explicit_session_name(tmp_path: Path) -> None:
    argv, raw = _run_ctl_attach(tmp_path, "main")

    assert raw == ["1", "1", "1"]
    assert argv[-3:] == ["attach-session", "-t", "main"]


def test_attach_creates_missing_workspace_through_daemon_before_attach(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "tmux.argv0"
    raw_log = tmp_path / "tmux.raw0"
    state = tmp_path / "has_session_count"
    curl_log = tmp_path / "curl.argv0"
    tmux = fakebin / "tmux"
    tmux.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\0' \"$@\" >> {log!s}\n"
        f"printf '%s\\0' \"${{IMPERIUM_TMUX_RAW:-}}\" >> {raw_log!s}\n"
        'case "$1" in\n'
        "  has-session)\n"
        f"    count=$(cat {state!s} 2>/dev/null || echo 0)\n"
        "    count=$((count + 1))\n"
        f"    printf '%s' \"$count\" > {state!s}\n"
        "    [[ $count -ge 2 ]] ;;\n"
        "  attach-session) exit 0 ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n"
    )
    tmux.chmod(0o755)
    curl = fakebin / "curl"
    curl.write_text(f"#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" > {curl_log!s}\n")
    curl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["TMUXCTLD_URL"] = "http://127.0.0.1:7778"
    env.pop("IMPERIUM_TMUX_RAW", None)

    result = _run_on_pty([str(CTL), "attach"], env=env)

    assert result.returncode == 0, result.stdout
    argv = [part.decode() for part in log.read_bytes().split(b"\0") if part]
    raw = [part.decode() for part in raw_log.read_bytes().split(b"\0") if part]
    curl_argv = [part.decode() for part in curl_log.read_bytes().split(b"\0") if part]
    assert raw == ["1", "1", "1"]
    assert argv == [
        "has-session",
        "-t",
        "main",
        "has-session",
        "-t",
        "main",
        "attach-session",
        "-t",
        "main",
    ]
    assert curl_argv[:4] == ["-sf", "-X", "POST", "http://127.0.0.1:7778/create"]
    assert '{"session":"main"}' in curl_argv


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
