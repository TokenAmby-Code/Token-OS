from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from tmuxctl.tmux_adapter import TmuxAdapter


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TMUX_SUBMIT_TIMING_INTEGRATION") != "1",
    reason="Set RUN_TMUX_SUBMIT_TIMING_INTEGRATION=1 to run deterministic tmux submit timing regression test.",
)


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], text=True, capture_output=True, check=check)


def test_text_then_submit_wait_prevents_newline_regression() -> None:
    """Deterministic tmux reproduction for the old back-to-back submit shape.

    The fake prompt mimics the observed Codex failure mode: text is accepted into
    an input buffer, but a submit key arriving too soon after the last byte is
    treated as a newline instead of dispatch. The fixed helper sends a delayed
    second C-m that submits; the old helper shape (literal text immediately
    followed by one C-m) leaves the prompt unsent.
    """

    if shutil.which("tmux") is None:
        pytest.skip("tmux binary not on PATH")

    session = f"submit-timing-test-{uuid.uuid4().hex[:8]}"
    script = r"""
import select
import sys
import termios
import time
import tty

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setcbreak(fd)
buf = ""
last_text_at = 0.0
sys.stdout.write("READY\n› ")
sys.stdout.flush()
try:
    while True:
        readable, _, _ = select.select([sys.stdin], [], [], 5)
        if not readable:
            continue
        ch = sys.stdin.read(1)
        now = time.monotonic()
        if ch in "\r\n":
            if now - last_text_at < 0.15:
                buf += "\n"
                sys.stdout.write("\\n")
                sys.stdout.flush()
                continue
            sys.stdout.write(f"\nSUBMITTED:{buf!r}\n")
            sys.stdout.flush()
            break
        buf += ch
        last_text_at = now
        sys.stdout.write(ch)
        sys.stdout.flush()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
"""

    try:
        _tmux("new-session", "-d", "-s", session, sys.executable, "-c", script)
        pane = _tmux("display-message", "-p", "-t", session, "#{pane_id}").stdout.strip()
        deadline = time.time() + 5
        while time.time() < deadline:
            if "READY" in _tmux("capture-pane", "-t", pane, "-p", check=False).stdout:
                break
            time.sleep(0.1)
        else:
            pytest.fail("fake prompt did not become ready")

        # Old behavior: text and submit back-to-back. This must not submit in the
        # fake TUI, proving the regression test catches the broken shape.
        _tmux("send-keys", "-t", pane, "-l", "old")
        _tmux("send-keys", "-t", pane, "C-m")
        time.sleep(0.4)
        capture = _tmux("capture-pane", "-t", pane, "-p", check=False).stdout
        assert "SUBMITTED" not in capture

        # Fixed helper: text, immediate C-m, settle, delayed C-m. This must submit.
        TmuxAdapter().send_text_then_submit(pane, "fixed")
        deadline = time.time() + 5
        while time.time() < deadline:
            capture = _tmux("capture-pane", "-t", pane, "-p", "-S", "-100", check=False).stdout
            if "SUBMITTED" in capture:
                assert "fixed" in capture
                return
            time.sleep(0.1)
        pytest.fail("fixed helper did not submit fake prompt")
    finally:
        _tmux("kill-session", "-t", session, check=False)
