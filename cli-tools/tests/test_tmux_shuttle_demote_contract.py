from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _demote_body() -> str:
    script = (ROOT / "bin" / "tmux-shuttle").read_text()
    return script.split("demote() {", 1)[1].split("\n}", 1)[0]


def test_demote_allocates_stack_shell_then_swaps() -> None:
    demote_body = _demote_body()

    # Demote must allocate the replacement through the unified stack allocator,
    # then swap. Creating the replacement after join-pane collapses the source
    # leaf and cannot reliably put the shell back in the original slot.
    assert "tmuxctl.cli stack add mechanicus" in demote_body
    assert 'tmux swap-pane -d -s "$current_pane" -t "$stack_pane"' in demote_body
    assert 'set-option -pu -t "$stack_pane" @PANE_TYPE' in demote_body
    assert "--no-focus" in demote_body


def test_demote_keeps_reaper_and_legacy_focus_out_of_path() -> None:
    demote_body = _demote_body()

    # The allocator owns stack geometry; the shuttle only swaps with the
    # allocator-created pane and restores metadata. It must not move panes by
    # raw join/move or fire audit reaping from the demote hot path.
    assert "stack adopt mechanicus" not in demote_body
    assert "join-pane" not in demote_body
    assert "move-pane" not in demote_body
    assert "tmux-audit" not in demote_body
    assert "mechanicus focus-selected" not in demote_body
