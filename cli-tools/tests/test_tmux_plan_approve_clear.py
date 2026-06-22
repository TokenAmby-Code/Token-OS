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


def test_codex_current_plan_modal_aborts_when_cursor_already_on_clear_context(
    tmp_path: pathlib.Path,
):
    fixture = tmp_path / "codex-current-on-option2.txt"
    fixture.write_text(
        "Implement this plan?\n\n"
        "  1. Yes, implement this plan          Switch to Default\n"
        "› 2. Yes, clear context and implement  Fresh thread.\n"
        "  3. No, stay in Plan mode             Continue planning\n"
    )
    out = subprocess.check_output(
        [str(SCRIPT), "--capture-file", str(fixture), "--agent", "codex", "--dry-run"],
        text=True,
    ).strip()
    assert out == "action=none timeout"


def test_codex_current_plan_modal_aborts_when_option_down_is_not_clear_context(
    tmp_path: pathlib.Path,
):
    fixture = tmp_path / "codex-current-option2-not-clear.txt"
    fixture.write_text(
        "Implement this plan?\n\n"
        "› 1. Yes, implement this plan          Switch to Default\n"
        "  2. Yes, implement without clearing   Keep context.\n"
        "  3. Yes, clear context and implement  Fresh thread.\n"
    )
    out = subprocess.check_output(
        [str(SCRIPT), "--capture-file", str(fixture), "--agent", "codex", "--dry-run"],
        text=True,
    ).strip()
    assert out == "action=none timeout"


def test_claude_ignores_stale_scrollback_modal_above_live_clear_context(
    tmp_path: pathlib.Path,
) -> None:
    # capture() reads -S -80, so a stale cursor-marked modal from a prior session
    # (here an "Exit anyway / Stay" prompt) can sit in scrollback ABOVE the live
    # clear-context modal. Anchoring on the FIRST cursor option used to pick the
    # stale "Exit anyway" line (no "clear context") -> classify "none" -> the modal
    # stuck and forced manual approval. The live modal is always at the bottom, so
    # we anchor on the LAST cursor option and must still send Enter.
    fixture = tmp_path / "claude-scrollback.txt"
    fixture.write_text(
        "  ❯ 1. Exit anyway\n"
        "    2. Stay\n"
        "\n"
        "tokenclaw@host ~ % claude\n"
        "\n"
        " Claude has written up a plan and is ready to execute. Would you\n"
        " like to proceed?\n"
        "\n"
        " ❯1.Yes, clear context (3% used) and\n"
        "    bypass permissions\n"
        "   2. Yes, and bypass permissions\n"
        "   3. Yes, manually approve edits\n"
    )
    out = subprocess.check_output(
        [str(SCRIPT), "--capture-file", str(fixture), "--agent", "claude", "--dry-run"],
        text=True,
    ).strip()
    assert out == "action=claude option-1 Enter"


def test_codex_ignores_stale_scrollback_modal_above_live_modal(
    tmp_path: pathlib.Path,
) -> None:
    # Same scrollback-shadow hazard for the Codex two-step: a stale top modal must
    # not capture the cursor anchor. The live modal's cursor sits on option 1 and
    # clear-context is one Down -> Down,Enter.
    fixture = tmp_path / "codex-scrollback.txt"
    fixture.write_text(
        "  ❯ 1. Exit anyway\n"
        "    2. Stay\n"
        "\n"
        "Implement this plan?\n"
        "\n"
        "› 1. Yes, implement this plan          Switch to Default\n"
        "  2. Yes, clear context and implement  Fresh thread.\n"
        "  3. No, stay in Plan mode             Continue planning\n"
    )
    out = subprocess.check_output(
        [str(SCRIPT), "--capture-file", str(fixture), "--agent", "codex", "--dry-run"],
        text=True,
    ).strip()
    assert out == "action=codex option-2 Down Enter"


def test_single_flight_lock_aborts_overlapping_watcher(tmp_path: pathlib.Path):
    safe = "%99".replace("%", "_")
    root = tmp_path / "tmux-plan-approve-clear"
    (root / f"{safe}.lockd").mkdir(parents=True)
    env = os.environ.copy()
    env["TMPDIR"] = str(tmp_path)
    out = subprocess.check_output(
        [str(SCRIPT), "--pane", "%99", "--agent", "codex", "--dry-run"],
        text=True,
        env=env,
    ).strip()
    assert out == "action=none locked"


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
        'case "$*" in\n'
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


def test_dry_run_reads_state_and_reports_not_planning(tmp_path: pathlib.Path) -> None:
    # Dry-run gates WRITES only, not the read-only state GET. With a real pane +
    # TOKEN_API_URL the watcher must read planning_state; when it is not planning,
    # the `state-not-planning` no-op branch is reachable (and observable) in dry-run.
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  *'-G'*) printf '%s\\n' '{\"success\":true,\"planning_state\":\"none\"}' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    (fakebin / "curl").chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["TOKEN_API_URL"] = "http://token-api.test"
    out = subprocess.check_output(
        [str(SCRIPT), "--pane", "%99", "--agent", "claude", "--dry-run"],
        text=True,
        env=env,
    ).strip()
    assert out == "action=none state-not-planning"


def test_non_clear_context_modal_sends_nothing(tmp_path):
    fixture = tmp_path / "other.txt"
    fixture.write_text("Approve running command?\n1. Yes\n2. No\n")
    out = subprocess.check_output(
        [str(SCRIPT), "--capture-file", str(fixture), "--timeout", "0", "--dry-run"],
        text=True,
    ).strip()
    assert out == "action=none timeout"
