from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONF = ROOT / "tmux" / "tmux-base.conf"


def _line_starting(prefix: str) -> str:
    for line in CONF.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"missing tmux binding: {prefix}")


def test_pane_select_prefix_arrows_use_native_low_latency_selection() -> None:
    for key, flag in {
        "Left": "-L",
        "Down": "-D",
        "Up": "-U",
        "Right": "-R",
    }.items():
        line = _line_starting(f"bind {key} ")
        assert "tmuxctl pane-select" not in line
        assert f"select-pane {flag}" in line
        assert "resize-pane -Z" in line
        assert "window_zoomed_flag" in line
        assert "switch-client -T pane-select" in line


def test_pane_select_prefix_hjkl_deexpand_before_routing() -> None:
    for key, direction in {
        "h": "left",
        "j": "down",
        "k": "up",
        "l": "right",
    }.items():
        line = _line_starting(f"bind {key} ")
        assert "resize-pane -Z" in line
        assert "window_zoomed_flag" in line
        assert "tmuxctl pane-select" in line
        assert "--mode relative" in line
        assert f"--direction {direction}" in line
        assert "switch-client -T pane-select" in line


def test_pane_select_enter_expands_without_status_flash() -> None:
    line = _line_starting("bind -T pane-select Enter ")
    assert "tmux-grid-expand" in line
    assert "--expand" in line
    assert "#{client_tty}" in line
    assert "display-message" not in line


def test_pane_select_table_arrows_are_bound_to_relative_routing() -> None:
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


def test_pane_select_bindings_do_not_use_timer_focus_override() -> None:
    pane_select_lines = [
        line
        for line in CONF.read_text(encoding="utf-8").splitlines()
        if "pane-select" in line and not line.lstrip().startswith("#")
    ]
    assert pane_select_lines
    assert all("--seconds" not in line for line in pane_select_lines)
    assert all("allow-mechanicus-focus" not in line for line in pane_select_lines)


def test_prefix_q_opens_mark_for_close_popup() -> None:
    conf = CONF.read_text(encoding="utf-8")
    line = _line_starting("bind Q ")
    assert "display-popup" in line
    assert "CLOSE_PANE=#{pane_id}" in line
    assert "tmux-mark-for-close" in line
    assert "unbind Q" not in conf
    # The command must be single-quoted so tmux does not expand $CLOSE_PANE at
    # config-parse time (which leaves --pane empty); the popup shell expands it.
    assert "'tmux-mark-for-close --pane \"$CLOSE_PANE\"'" in line

    script = (ROOT / "bin" / "tmux-mark-for-close").read_text(encoding="utf-8")
    assert "/api/instances/${INSTANCE_ID}/mark-for-close" in script
    assert "/api/hooks/subscribe" not in script
    assert "/api/hooks/unsubscribe" in script
    assert "_unsubscribe_mark" in script
    assert "send-text --pane" in script
    assert "kill-pane" not in script
    assert "/retire" not in script
    assert "/archive-session-doc" not in script


def test_mark_for_close_script_is_committed_executable() -> None:
    # core.fileMode is false in this repo, so a missing exec bit is not caught by
    # the working tree; assert the committed git mode is 100755 directly. The
    # tmux popup runs `tmux-mark-for-close` off PATH and a non-exec file fails.
    out = subprocess.run(
        ["git", "ls-files", "-s", "bin/tmux-mark-for-close"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout, "tmux-mark-for-close is not tracked by git"
    mode = out.stdout.split()[0]
    assert mode == "100755", f"tmux-mark-for-close must be committed executable, got {mode}"
