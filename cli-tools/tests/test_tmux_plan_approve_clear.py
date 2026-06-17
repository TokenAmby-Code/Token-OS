from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "tmux-plan-approve-clear"


def test_claude_clear_context_modal_sends_enter(tmp_path):
    fixture = tmp_path / "claude.txt"
    fixture.write_text(
        "> 1. Yes, clear context and auto-accept edits (shift+tab)\n  2. Yes, and manually approve edits\n"
    )
    out = subprocess.check_output(
        [str(SCRIPT), "--capture-file", str(fixture), "--agent", "claude", "--dry-run"],
        text=True,
    ).strip()
    assert out == "action=claude option-1 Enter"


def test_codex_clear_context_modal_sends_option_two_sequence(tmp_path):
    fixture = tmp_path / "codex.txt"
    fixture.write_text(
        "Codex approval\n> 1. Clear context and auto approve edits\n  2. Clear context and manually approve edits\n"
    )
    out = subprocess.check_output(
        [str(SCRIPT), "--capture-file", str(fixture), "--agent", "codex", "--dry-run"],
        text=True,
    ).strip()
    assert out == "action=codex option-2 Down Enter"


def test_codex_current_plan_modal_sends_option_two_sequence(tmp_path: pathlib.Path):
    fixture = tmp_path / "codex-current.txt"
    fixture.write_text(
        "Implement this plan?\n\n"
        "› 1. Yes, implement this plan          Switch to Default\n"
        "                                       and start coding.\n"
        "  2. Yes, clear context and implement  Fresh thread.\n"
        "                                       Context: 1% used.\n"
        "  3. No, stay in Plan mode             Continue planning\n"
        "                                       with the model.\n"
        "\n"
        "Press enter to confirm or esc to go back\n"
    )
    out = subprocess.check_output(
        [str(SCRIPT), "--capture-file", str(fixture), "--agent", "codex", "--dry-run"],
        text=True,
    ).strip()
    assert out == "action=codex option-2 Down Enter"


def test_successful_click_leaves_state_for_session_start(tmp_path):
    fixture = tmp_path / "pane.txt"
    fixture.write_text(
        "Codex approval\n> 1. Clear context and auto approve edits\n  2. Clear context and manually approve edits\n"
    )
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    curl_log = tmp_path / "curl.log"
    tmux_log = tmp_path / "tmux.log"
    (fakebin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {curl_log!s}\n"
        "case \"$*\" in\n"
        "  *'-G'*) printf '%s\\n' '{\"success\":true,\"planning_state\":\"planning\"}' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    (fakebin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f"  capture-pane) cat {fixture!s} ;;\n"
        f"  send-keys) printf '%s\\n' \"$*\" >> {tmux_log!s} ;;\n"
        "esac\n"
    )
    for script in fakebin.iterdir():
        script.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["TOKEN_API_URL"] = "http://token-api.test"
    subprocess.check_call(
        [str(SCRIPT), "--pane", "%99", "--agent", "codex", "--timeout", "1"],
        env=env,
    )

    body_log = curl_log.read_text()
    assert '"state":"approving"' in body_log
    assert '"state":"none"' not in body_log
    assert tmux_log.read_text().strip() == "send-keys -t %99 Down Enter"


def test_non_clear_context_modal_sends_nothing(tmp_path):
    fixture = tmp_path / "other.txt"
    fixture.write_text("Approve running command?\n1. Yes\n2. No\n")
    out = subprocess.check_output(
        [str(SCRIPT), "--capture-file", str(fixture), "--timeout", "0", "--dry-run"],
        text=True,
    ).strip()
    assert out == "action=none timeout"
