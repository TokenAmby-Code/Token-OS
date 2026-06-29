from __future__ import annotations

import fcntl
import os
import pathlib
import subprocess
import time

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


def test_classifier_none_with_plan_tokens_logs_once_per_watcher(
    tmp_path: pathlib.Path,
) -> None:
    fixture = tmp_path / "plan-token-no-modal.txt"
    fixture.write_text("Implement this plan after reviewing the proposed steps.\n")
    proc = subprocess.run(
        [
            str(SCRIPT),
            "--capture-file",
            str(fixture),
            "--agent",
            "codex",
            "--timeout",
            "1",
            "--interval",
            "0.1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert proc.stdout == ""
    assert proc.stderr.count("result=classifier-none-with-plan-tokens") == 1
    assert "result=timeout" in proc.stderr


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


def test_single_flight_lock_aborts_overlapping_watcher(tmp_path: pathlib.Path) -> None:
    # The script's single-flight guard prefers flock (a `.lock` FILE) when the
    # `flock` binary is available (Linux/CI) and only falls back to an atomic
    # mkdir (`.lockd` DIR) when it is not (stock macOS). To assert "locked"
    # deterministically on BOTH backends, hold whichever lock the script will
    # actually contend on: an exclusive flock on the `.lock` file *and* a
    # pre-created `.lockd` directory. (Previously this test created only the
    # `.lockd` dir, so on CI the script took the flock path, acquired the lock
    # uncontended, and fell through to `state-not-planning` — a platform-dependent
    # failure, not a flake.) fcntl.flock contends with shell `flock(1)` because
    # both use the flock(2) syscall.
    safe = "%99".replace("%", "_")  # mirrors the script's `tr -c 'A-Za-z0-9_.-' '_'`
    root = tmp_path / f"tmux-plan-approve-clear-{os.getuid()}"
    root.mkdir(parents=True, mode=0o700)
    (root / f"{safe}.lockd").mkdir()  # mkdir-fallback backend (macOS)
    env = os.environ.copy()
    env["TMPDIR"] = str(tmp_path)
    with open(root / f"{safe}.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # flock backend (Linux/CI)
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
        f'printf \'%s\\n\' "$*" >> "{curl_log!s}"\n'
        'case "$*" in\n'
        "  *'-G'*) printf '%s\\n' '{\"success\":true,\"planning_state\":\"planning\"}' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    (fakebin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f"  capture-pane) cat {fixture!s} ;;\n"
        f'  send-keys) printf \'%s\\n\' "$*" >> "{tmux_log!s}" ;;\n'
        "esac\n"
    )
    for script in fakebin.iterdir():
        script.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["TOKEN_API_TMUX_BIN"] = str(fakebin / "tmux")
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
    (fakebin / "tmux").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fakebin / "tmux").chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["TOKEN_API_TMUX_BIN"] = str(fakebin / "tmux")
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


def test_codex_truncated_current_plan_modal_accepts_fresh_thread_option(
    tmp_path: pathlib.Path,
) -> None:
    fixture = tmp_path / "codex-truncated-current.txt"
    fixture.write_text(
        "Implement this plan?\n\n"
        "› 1. Yes, implement t… Switch to\n"
        "                       Default and\n"
        "                       start\n"
        "                       coding.\n"
        "  2. Yes, clear conte… Fresh\n"
        "                       thread.\n"
        "                       Context: 6%\n"
        "                       used.\n"
        "  3. No, stay in Plan… Continue\n"
        "                       planning\n"
        "                       with the\n"
        "                       model.\n"
    )
    out = subprocess.check_output(
        [str(SCRIPT), "--capture-file", str(fixture), "--agent", "codex", "--dry-run"],
        text=True,
    ).strip()
    assert out == "action=codex option-2 Down Enter"


def test_lease_extension_keeps_locked_watcher_alive_past_original_timeout(
    tmp_path: pathlib.Path,
) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "tmux").write_text(
        "#!/usr/bin/env bash\ncase \"$1\" in\n  capture-pane) printf 'no modal yet\\n' ;;\nesac\n"
    )
    (fakebin / "tmux").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["TOKEN_API_TMUX_BIN"] = str(fakebin / "tmux")
    env["TMPDIR"] = str(tmp_path)
    root = tmp_path / f"tmux-plan-approve-clear-{os.getuid()}"
    root.mkdir(mode=0o700)
    lease = root / "_99.deadline"
    start = time.monotonic()
    proc = subprocess.Popen(
        [str(SCRIPT), "--pane", "%99", "--agent", "codex", "--timeout", "1", "--no-state"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.4)
    lease_tmp = lease.with_name(f"{lease.name}.tmp")
    lease_tmp.write_text(str(int(time.time() + 2)))
    os.replace(lease_tmp, lease)
    stdout, stderr = proc.communicate(timeout=5)
    elapsed = time.monotonic() - start
    assert proc.returncode == 0
    assert stdout == ""
    # The lease deadline is written as int(time.time() + 2), so integer-second
    # truncation makes the real elapsed floor ~1.4s (worst-case sub-second
    # alignment), not 2s. We only need to prove the watcher outlived its
    # original 1s --timeout; assert comfortably above 1.0 but below the ~1.4s
    # truncation floor so contended-runner jitter can't tip it red.
    assert elapsed >= 1.2
    assert "result=timeout" in stderr


def test_positional_pane_target_resolves_in_consumer_not_launcher(tmp_path: pathlib.Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    tmux_log = tmp_path / "tmux.log"
    (fakebin / "tmuxctl").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "resolve-pane" ]]; then printf "%%55\\n"; exit 0; fi\n'
        "exit 1\n"
    )
    (fakebin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  display-message) exit 1 ;;\n"
        "  capture-pane) printf 'Implement this plan?\\n› 1. Yes, implement this plan\\n  2. Yes, clear context and implement  Fresh thread.\\n' ;;\n"
        f'  send-keys) printf \'%s\\n\' "$*" >> "{tmux_log!s}" ;;\n'
        "esac\n"
    )
    for script in fakebin.iterdir():
        script.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["TOKEN_API_TMUX_BIN"] = str(fakebin / "tmux")
    subprocess.check_call(
        [str(SCRIPT), "--pane", "somnium:SE", "--agent", "codex", "--timeout", "1", "--no-state"],
        env=env,
    )
    assert tmux_log.read_text().strip() == "send-keys -t %55 Down Enter"


def test_instance_id_state_calls_avoid_tmux_pane_payload(tmp_path: pathlib.Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    curl_log = tmp_path / "curl.log"
    tmux_log = tmp_path / "tmux.log"
    (fakebin / "tmuxctl").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "resolve-instance" ]]; then printf "%%56\\n"; exit 0; fi\n'
        "exit 1\n"
    )
    (fakebin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$*" >> "{curl_log!s}"\n'
        'cat >> "' + str(curl_log) + '"\n'
        'case "$*" in\n'
        "  *'-G'*) printf '%s\\n' '{\"success\":true,\"planning_state\":\"planning\"}' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    (fakebin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  display-message) exit 1 ;;\n"
        "  capture-pane) printf 'Implement this plan?\\n› 1. Yes, implement this plan\\n  2. Yes, clear context and implement  Fresh thread.\\n' ;;\n"
        f'  send-keys) printf \'%s\\n\' "$*" >> "{tmux_log!s}" ;;\n'
        "esac\n"
    )
    for script in fakebin.iterdir():
        script.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["TOKEN_API_TMUX_BIN"] = str(fakebin / "tmux")
    env["TOKEN_API_URL"] = "http://token-api.test"
    subprocess.check_call(
        [str(SCRIPT), "--instance-id", "api-instance-1", "--agent", "codex", "--timeout", "1"],
        env=env,
    )
    curl_text = curl_log.read_text()
    assert "instance_id=api-instance-1" in curl_text
    assert "tmux_pane" not in curl_text
    assert tmux_log.read_text().strip() == "send-keys -t %56 Down Enter"
