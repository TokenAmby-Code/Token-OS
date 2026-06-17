from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONF = ROOT / "tmux" / "tmux-base.conf"


def _line_starting(prefix: str) -> str:
    for line in CONF.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"missing tmux binding: {prefix}")


def test_pane_select_prefix_arrows_are_bound_to_absolute_routing():
    for key, direction in {
        "Left": "left",
        "Down": "down",
        "Up": "up",
        "Right": "right",
    }.items():
        line = _line_starting(f"bind {key} ")
        assert "tmuxctl pane-select" in line
        assert "--mode absolute" in line
        assert f"--direction {direction}" in line
        assert "switch-client -T pane-select" in line


def test_pane_select_table_arrows_are_bound_to_relative_routing():
    for key, direction in {
        "Left": "left",
        "Down": "down",
        "Up": "up",
        "Right": "right",
    }.items():
        line = _line_starting(f"bind -T pane-select {key} ")
        assert "tmuxctl pane-select" in line
        assert "--mode relative" in line
        assert f"--direction {direction}" in line
        assert "switch-client -T pane-select" in line


def test_pane_select_bindings_do_not_use_timer_focus_override():
    pane_select_lines = [
        line
        for line in CONF.read_text(encoding="utf-8").splitlines()
        if "pane-select" in line and not line.lstrip().startswith("#")
    ]
    assert pane_select_lines
    assert all("--seconds" not in line for line in pane_select_lines)
    assert all("allow-mechanicus-focus" not in line for line in pane_select_lines)


def test_prefix_q_opens_mark_for_close_popup():
    conf = CONF.read_text(encoding="utf-8")
    line = _line_starting("bind Q ")
    assert "display-popup" in line
    assert "CLOSE_PANE=#{pane_id}" in line
    assert "tmux-mark-for-close" in line
    assert "unbind Q" not in conf

    script = (ROOT / "bin" / "tmux-mark-for-close").read_text(encoding="utf-8")
    assert "/api/instances/${INSTANCE_ID}/mark-for-close" in script
    assert "/api/hooks/subscribe" not in script
    assert "/api/hooks/unsubscribe" in script
    assert "_unsubscribe_mark" in script
    assert "send-text --pane" in script
    assert "kill-pane" not in script
    assert "/retire" not in script
    assert "/archive-session-doc" not in script
