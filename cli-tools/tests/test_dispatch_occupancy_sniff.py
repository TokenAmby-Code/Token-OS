from __future__ import annotations

from pathlib import Path


def _assert_dispatch_target_unoccupied_body() -> str:
    script = Path(__file__).resolve().parents[1] / "bin" / "dispatch"
    text = script.read_text()
    start = text.index("assert_dispatch_target_unoccupied() {")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def test_dispatch_occupancy_sniff_does_not_join_freeform_pane_title() -> None:
    body = _assert_dispatch_target_unoccupied_body()

    assert (
        "#{pane_current_command}|#{@PANE_ID}|#{pane_pid}|#{@INSTANCE_ID}|#{pane_title}" not in body
    )
    assert "#{pane_current_command}|#{@PANE_ID}|#{pane_pid}|#{@INSTANCE_ID}" in body


def test_dispatch_occupancy_sniff_title_is_defined_without_joining_metadata() -> None:
    body = _assert_dispatch_target_unoccupied_body()

    assert "local pane meta cmd pane_label pane_pid instance_id title" in body
    assert 'title="$(tmux display-message -p -t "$pane" \'#{pane_title}\'' in body
    assert (
        "#{pane_current_command}|#{@PANE_ID}|#{pane_pid}|#{@INSTANCE_ID}|#{pane_title}" not in body
    )
