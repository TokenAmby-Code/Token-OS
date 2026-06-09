from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_demote_routes_through_managed_stack_allocation() -> None:
    script = (ROOT / "bin" / "tmux-shuttle").read_text()
    demote_body = script.split("demote() {", 1)[1].split("\n}", 1)[0]

    assert "tmuxctl.cli stack add legion" in demote_body
    assert "--no-focus" in demote_body
    assert 'tmux move-pane -s "$current_pane"' not in demote_body
    assert '@PANE_TYPE "stack-worker"' in demote_body
