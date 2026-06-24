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


def test_pane_select_prefix_arrows_use_stack_aware_absolute_selection() -> None:
    for key, (pane_index, direction) in {
        "Left": ("1", "left"),
        "Up": ("2", "up"),
        "Down": ("3", "down"),
        "Right": ("4", "right"),
    }.items():
        line = _line_starting(f"bind {key} ")
        assert f"#{'{session_name}'}:#{'{window_index}'}.{pane_index}" in line
        assert "tmuxctl pane-select --mode absolute" in line
        assert f"--direction {direction}" in line
        assert "m:mechanicus*,#{window_name}" in line
        # The retired per-fleet stack-page globs must not reappear in the matcher.
        assert "m:legion*" not in line
        assert "select-pane -L" not in line
        assert "select-pane -R" not in line
        assert "select-pane -U" not in line
        assert "select-pane -D" not in line
        assert "resize-pane -Z" in line
        assert "window_zoomed_flag" in line
        assert "switch-client -T pane-select" in line


def test_pane_select_prefix_arrows_keep_native_non_stack_targets() -> None:
    for key, pane_index in {
        "Left": "1",
        "Up": "2",
        "Down": "3",
        "Right": "4",
    }.items():
        line = _line_starting(f"bind {key} ")
        native = f"'select-pane -t \"#{{session_name}}:#{{window_index}}.{pane_index}\"'"
        assert native in line


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
    assert all("stack enforce" not in line for line in pane_select_lines)
    assert all("@STACK_FOCUSED_PANE" not in line for line in pane_select_lines)


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
    assert "_begin_atomic_close_contract" in script
    assert "trap '' INT QUIT TSTP" in script
    assert "MARK_FOR_CLOSE_OK" in script
    assert "send-text --pane" in script
    assert "kill-pane" not in script
    assert "/retire" not in script
    assert "/archive-session-doc" not in script

    exit_script = (ROOT / "bin" / "tmux-instance-exit").read_text(encoding="utf-8")
    assert '"$TMUXCTL_BIN" close --instance-id' in exit_script
    assert "trap '' INT QUIT TSTP" in exit_script
    assert "CLOSE_CONTRACT_OK" in exit_script
    assert "send-keys C-c" not in exit_script


def test_mark_for_close_script_is_committed_executable() -> None:
    # The canonical bare is core.fileMode=true (this repo ships executables), but
    # a checkout's working-tree exec bit can still be unreliable on filesystems
    # that don't honor it (e.g. SMB/NAS shares). So we assert the *committed* git
    # index mode is 100755 directly — robust regardless of the checkout
    # filesystem's exec-bit fidelity. The tmux popup runs `tmux-mark-for-close`
    # off PATH and a non-exec file fails.
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


def _setting(path: pathlib.Path, prefix: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"missing setting: {prefix}")


def test_typing_guard_status_is_pure_format_no_status_poll() -> None:
    status_right = _setting(CONF, "set -g status-right ")
    assert "tmux-typing-guard-status" not in status_right
    assert "--scan" not in status_right
    assert "#(" not in status_right
    assert "#{?@GUARD" in status_right

    border = _setting(CONF, "set -g pane-border-format ")
    assert "#{?@GUARD" in border
    assert "#(" not in border


def test_portable_typing_guard_status_is_pure_format_no_status_poll() -> None:
    portable = ROOT / "tmux" / "tmux-portable-status.conf"
    status_right = _setting(portable, "set status-right ")
    assert "tmux-typing-guard-status" not in status_right
    assert "--scan" not in status_right
    assert "#(" not in status_right
    assert "#{?@GUARD" in status_right


def test_typing_guard_key_events_publish_and_clear_guard_marker() -> None:
    conf = CONF.read_text(encoding="utf-8")
    assert "bind -n Any" in conf
    assert "set -Fp @TYPING_LOCK_UNTIL" in conf
    assert "set -p @GUARD" in conf
    assert "tmux-typing-guard-status --clear-expired -t #{pane_id}" in conf
    assert "bind -n Enter { set -pu @TYPING_LOCK_UNTIL ; set -pu @GUARD ; send-keys }" in conf
    assert "bind -n C-m { set -pu @TYPING_LOCK_UNTIL ; set -pu @GUARD ; send-keys }" in conf
