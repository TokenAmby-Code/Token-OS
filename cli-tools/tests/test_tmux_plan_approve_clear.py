from __future__ import annotations

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


def test_non_clear_context_modal_sends_nothing(tmp_path):
    fixture = tmp_path / "other.txt"
    fixture.write_text("Approve running command?\n1. Yes\n2. No\n")
    out = subprocess.check_output(
        [str(SCRIPT), "--capture-file", str(fixture), "--timeout", "0", "--dry-run"],
        text=True,
    ).strip()
    assert out == "action=none timeout"
