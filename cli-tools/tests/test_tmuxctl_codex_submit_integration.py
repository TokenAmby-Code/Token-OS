from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from tmuxctl.tmux_adapter import TmuxAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TMUX_CODEX_SUBMIT_INTEGRATION") != "1",
    reason="Set RUN_TMUX_CODEX_SUBMIT_INTEGRATION=1 to run live Codex/tmux submit regression test.",
)


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], text=True, capture_output=True, check=check)


def test_codex_tui_text_then_submit_dispatches() -> None:
    """Live regression test for Codex prompt submission through tmux.

    This intentionally drives a real Codex TUI in a sacrificial tmux session. It
    is env-gated because it requires Codex auth/network and consumes a small
    model turn. The pre-2026-05-10 helper sent text and C-m back-to-back; on the
    live regression panes that left prompts queued. The fixed helper waits before
    C-m, matching claude-cmd's proven path.
    """

    if shutil.which("codex") is None:
        pytest.skip("codex binary not on PATH")
    if shutil.which("tmux") is None:
        pytest.skip("tmux binary not on PATH")

    session = f"codex-submit-test-{uuid.uuid4().hex[:8]}"
    token = f"CODEX_SUBMIT_OK_{uuid.uuid4().hex[:8]}"
    cwd = Path(__file__).resolve().parents[2]

    try:
        _tmux(
            "new-session",
            "-d",
            "-s",
            session,
            "-c",
            str(cwd),
            "codex -C . --dangerously-bypass-approvals-and-sandbox",
        )
        pane = _tmux("display-message", "-p", "-t", session, "#{pane_id}").stdout.strip()

        deadline = time.time() + 30
        while time.time() < deadline:
            capture = _tmux("capture-pane", "-t", pane, "-p", check=False).stdout
            if "›" in capture or ">" in capture:
                break
            time.sleep(0.5)
        else:
            pytest.fail("Codex TUI prompt did not become ready")

        prompt = f"Reply exactly {token} and do not use tools."
        TmuxAdapter().send_text_then_submit(pane, prompt)

        deadline = time.time() + 90
        while time.time() < deadline:
            capture = _tmux("capture-pane", "-t", pane, "-p", "-S", "-200", check=False).stdout
            if token in capture:
                return
            time.sleep(1)
        pytest.fail(f"Codex did not process submitted prompt; token {token!r} absent from pane")
    finally:
        _tmux("kill-session", "-t", session, check=False)
