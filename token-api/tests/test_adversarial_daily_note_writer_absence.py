"""Adversarial pins that keep automatic Stop-hook daily-note mutation dead."""

from pathlib import Path


def test_stop_hook_has_no_daily_note_fallback_writer():
    source = (Path(__file__).resolve().parents[1] / "stop_hook.py").read_text()

    assert "append_wikilink_to_daily_note" not in source
    assert "No session doc — linking transcript to daily note" not in source
    assert r"Terra/Journal/Daily/\d{4}-\d{2}-\d{2}\.md$" in source
    assert "Mac daily-note transcript link skipped" in source
