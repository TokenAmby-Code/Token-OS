from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _demote_body() -> str:
    script = (ROOT / "bin" / "tmux-shuttle").read_text()
    return script.split("demote() {", 1)[1].split("\n}", 1)[0]


def test_demote_adopts_live_pane_through_unified_stack_path() -> None:
    demote_body = _demote_body()

    # Demote routes the LIVE pane into the legion stack via the unified
    # allocator's adopt path — no demotion-specific stack management.
    assert "tmuxctl.cli stack adopt legion" in demote_body
    assert "--pane" in demote_body
    assert "--no-focus" in demote_body


def test_demote_has_no_bespoke_stack_surgery() -> None:
    demote_body = _demote_body()

    # The allocator owns geometry, tagging, pending flags, and the reap. The
    # shuttle must not swap panes, move panes, retag stack identity, or fire the
    # workspace audit (which previously culled Custodes).
    assert "swap-pane" not in demote_body
    assert "move-pane" not in demote_body
    assert "tmux-audit" not in demote_body
    assert "legion focus-selected" not in demote_body
    # Worker typing is the allocator's job now, not the shuttle's.
    assert '@PANE_TYPE "stack-worker"' not in demote_body
