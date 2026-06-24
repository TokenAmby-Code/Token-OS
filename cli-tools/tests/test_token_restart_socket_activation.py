"""Tests for the launchd socket-activation reconcile + graceful-drain restart.

The fix makes launchd own the port-7777 listening socket (its accept backlog
survives uvicorn restarts, so a deploy stalls hooks in the kernel instead of
connection-refusing them). These tests cover the reproducible, repo-controlled
pieces: the in-place plist reconciler, the launchd pid lookup that replaces the
now-stale `pgrep -f uvicorn.*7777`, and the canonical template.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

CLI_TOOLS = Path(__file__).resolve().parents[1]
TOKEN_RESTART = CLI_TOOLS / "bin" / "token-restart"
TEMPLATE = CLI_TOOLS / "launchd" / "ai.openclaw.tokenapi.plist"

# Legacy live-plist shape this upgrade migrates FROM: `-m uvicorn …`, no Sockets.
LEGACY_PLIST = {
    "Label": "ai.openclaw.tokenapi",
    "ProgramArguments": [
        "/Users/tokenclaw/.local/venvs/token-api/bin/python",
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "7777",
    ],
    "EnvironmentVariables": {
        "HOME": "/Users/tokenclaw",
        "CD_RESTART_SECRET": "deadbeef-secret",
    },
    "SoftResourceLimits": {"NumberOfFiles": 16384},
    "HardResourceLimits": {"NumberOfFiles": 32768},
    "KeepAlive": {"SuccessfulExit": False},
}


def _source_and_run(snippet: str, *, plist: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Source token-restart (functions only — the dispatch is BASH_SOURCE-guarded)
    and run a snippet against its functions."""
    pre = "set +e\n"
    if plist is not None:
        pre += f"PLIST={str(plist)!r}\n"
    script = f"source {str(TOKEN_RESTART)!r}\n{pre}{snippet}\n"
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True)


def test_reconciler_migrates_legacy_uvicorn_plist(tmp_path: Path) -> None:
    p = tmp_path / "ai.openclaw.tokenapi.plist"
    with p.open("wb") as f:
        plistlib.dump(LEGACY_PLIST, f)

    res = _source_and_run("ensure_plist_socket_activation && echo RC=0 || echo RC=$?", plist=p)
    assert "RC=1" in res.stdout, res.stderr  # 1 => modified

    data = plistlib.load(p.open("rb"))
    # ProgramArguments collapsed to [<python>, main.py] so main.py's shim runs.
    assert data["ProgramArguments"] == [
        "/Users/tokenclaw/.local/venvs/token-api/bin/python",
        "main.py",
    ]
    # Sockets dict added so launchd owns the listening socket.
    assert data["Sockets"] == {
        "Listeners": {
            "SockServiceName": "7777",
            "SockType": "stream",
            "SockFamily": "IPv4",
        }
    }
    # Secrets and fd limits preserved (reconciler never touches them).
    assert data["EnvironmentVariables"]["CD_RESTART_SECRET"] == "deadbeef-secret"
    assert data["SoftResourceLimits"]["NumberOfFiles"] == 16384
    assert data["HardResourceLimits"]["NumberOfFiles"] == 32768


def test_reconciler_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "ai.openclaw.tokenapi.plist"
    with p.open("wb") as f:
        plistlib.dump(LEGACY_PLIST, f)

    first = _source_and_run("ensure_plist_socket_activation && echo RC=0 || echo RC=$?", plist=p)
    assert "RC=1" in first.stdout, first.stderr
    second = _source_and_run("ensure_plist_socket_activation && echo RC=0 || echo RC=$?", plist=p)
    assert "RC=0" in second.stdout, second.stderr  # 0 => already canonical, no-op


def test_reconciler_preserves_custom_interpreter(tmp_path: Path) -> None:
    """`--from <dir>` sets a worktree venv python; the reconciler must keep it."""
    custom = dict(LEGACY_PLIST)
    custom["ProgramArguments"] = ["/tmp/wt/.venv/bin/python", "-m", "uvicorn", "main:app"]
    p = tmp_path / "ai.openclaw.tokenapi.plist"
    with p.open("wb") as f:
        plistlib.dump(custom, f)

    _source_and_run("ensure_plist_socket_activation || true", plist=p)
    data = plistlib.load(p.open("rb"))
    assert data["ProgramArguments"] == ["/tmp/wt/.venv/bin/python", "main.py"]


def test_mac_server_pid_parses_launchctl(tmp_path: Path) -> None:
    stub = tmp_path / "stubbin"
    stub.mkdir()
    launchctl = stub / "launchctl"
    launchctl.write_text(
        "#!/usr/bin/env bash\n"
        'cat <<"OUT"\n'
        "ai.openclaw.tokenapi = {\n"
        "\tactive count = 1\n"
        "\tpid = 54321\n"
        "\tprogram = /Users/x/python\n"
        "}\n"
        "OUT\n"
    )
    launchctl.chmod(0o755)

    script = f"source {str(TOKEN_RESTART)!r}\nset +e\nmac_server_pid\n"
    res = subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        env={"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert res.stdout.strip() == "54321", res.stderr


def test_no_stale_uvicorn_process_pattern() -> None:
    """The process now runs as `python main.py`; the old uvicorn pgrep/pkill
    pattern must be gone or it silently fails to find / kill the server."""
    src = TOKEN_RESTART.read_text(encoding="utf-8")
    assert "uvicorn.*7777" not in src


def test_restart_mac_uses_graceful_sigterm() -> None:
    src = TOKEN_RESTART.read_text(encoding="utf-8")
    # Graceful drain via SIGTERM to the launchd pid, not `kickstart -k` (SIGKILL).
    assert "kill -TERM" in src
    assert "kickstart -k gui/501/${LABEL}" not in src


def test_canonical_template_is_socket_activated() -> None:
    data = plistlib.load(TEMPLATE.open("rb"))
    assert data["ProgramArguments"][-1] == "main.py"
    assert "uvicorn" not in data["ProgramArguments"]
    assert data["Sockets"]["Listeners"]["SockServiceName"] == "7777"
    assert data["Sockets"]["Listeners"]["SockType"] == "stream"
