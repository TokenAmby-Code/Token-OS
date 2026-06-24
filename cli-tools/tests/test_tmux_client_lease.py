from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tmux_client_lease import (
    ROLE_DESKTOP,
    ROLE_MOBILE,
    ROLE_UNKNOWN,
    Client,
    classify_client,
    lease_decision,
)


def test_classifies_ghostty_as_desktop() -> None:
    client = Client(tty="/dev/ttys001", termname="xterm-ghostty", pid="100", session="main")
    assert classify_client(client) == ROLE_DESKTOP


def test_classifies_marked_phone_ssh_as_mobile() -> None:
    client = Client(
        tty="/dev/ttys010",
        termname="screen-256color",
        pid="200",
        session="main",
        role_marker="mobile",
    )
    assert classify_client(client, process_chain=("sshd", "launchd")) == ROLE_MOBILE


def test_classifies_phone_grouped_session_as_mobile() -> None:
    client = Client(tty="/dev/ttys011", termname="screen-256color", pid="201", session="phone")
    assert classify_client(client) == ROLE_MOBILE


def test_unknown_client_remains_unknown() -> None:
    client = Client(tty="/dev/ttys099", termname="vt100", pid="300", session="main")
    assert classify_client(client) == ROLE_UNKNOWN


def test_mobile_activity_detaches_desktop_only() -> None:
    clients = (
        Client("/dev/desk", termname="xterm-ghostty", session="main"),
        Client("/dev/phone", session="phone"),
        Client("/dev/rescue", termname="vt100", session="main"),
    )
    decision = lease_decision(clients, ROLE_MOBILE, now=1000, protected_until={})
    assert decision.detach_ttys == ("/dev/desk",)


def test_desktop_activity_detaches_mobile_only() -> None:
    clients = (
        Client("/dev/desk", termname="xterm-ghostty", session="main"),
        Client("/dev/phone", session="phone"),
        Client("/dev/rescue", termname="vt100", session="main"),
    )
    decision = lease_decision(clients, ROLE_DESKTOP, now=1000, protected_until={})
    assert decision.detach_ttys == ("/dev/phone",)


def test_protected_opposite_role_is_spared() -> None:
    clients = (
        Client("/dev/desk", termname="xterm-ghostty", session="main"),
        Client("/dev/phone", session="phone"),
    )
    decision = lease_decision(
        clients,
        ROLE_MOBILE,
        now=1000,
        protected_until={ROLE_DESKTOP: 1300},
    )
    assert decision.detach_ttys == ()


def test_never_detaches_only_client() -> None:
    clients = (Client("/dev/desk", termname="xterm-ghostty", session="main"),)
    decision = lease_decision(clients, ROLE_MOBILE, now=1000, protected_until={})
    assert decision.detach_ttys == ()


def test_unknown_active_role_is_noop() -> None:
    clients = (
        Client("/dev/desk", termname="xterm-ghostty", session="main"),
        Client("/dev/phone", session="phone"),
    )
    decision = lease_decision(clients, ROLE_UNKNOWN, now=1000, protected_until={})
    assert decision.detach_ttys == ()


def test_cli_activity_detaches_opposite_role_with_fake_tmux(tmp_path: Path) -> None:
    log = tmp_path / "tmux.log"
    fake_tmux = tmp_path / "tmux"
    fake_tmux.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' \"$*\" >> {str(log)!r}
case \"${{1:-}}\" in
  list-clients)
    printf '/dev/desk\\txterm-ghostty\\t111\\tmain\\t100\\n'
    printf '/dev/phone\\tscreen-256color\\t222\\tphone\\t100\\n'
    ;;
  show-options)
    exit 1
    ;;
  set-option|detach-client)
    ;;
  *)
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)
    env = os.environ.copy()
    env["IMPERIUM_TMUX_BIN"] = str(fake_tmux)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "lib")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tmux_client_lease",
            "activity",
            "--role",
            "mobile",
            "--client",
            "/dev/phone",
            "--session",
            "main",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    text = log.read_text(encoding="utf-8")
    assert "detach-client -t /dev/desk" in text
    assert "detach-client -t /dev/phone" not in text


def test_bin_wrapper_runs_on_platform_without_readlink_f() -> None:
    wrapper = Path(__file__).resolve().parents[1] / "bin" / "tmux-client-lease"
    result = subprocess.run(
        [str(wrapper), "--dry-run", "protect", "mobile", "0"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "protected mobile" in result.stdout
