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


def test_typing_guard_indicator_is_per_pane_not_global_taskbar() -> None:
    """The typing-guard indicator lives in each pane's border, not the global bar.

    Emperor UX directive: drop the global "⌨ GUARD" status-right segment and show
    guard state per-pane via @GUARD. status-right keeps tmux-typing-guard-status
    only as a SILENT --scan reconciler (one fork/interval, no visible segment) so
    every pane's @GUARD is refreshed honestly and stale markers clear.
    """
    status_right = _line_starting("set -g status-right ")
    # The reconciler must run in --scan mode (all panes), never the current-pane
    # visible-segment mode.
    assert "tmux-typing-guard-status --scan" in status_right
    assert "#(tmux-typing-guard-status 2>/dev/null)" not in status_right, (
        "the visible current-pane guard segment must be removed from the global bar"
    )
    # The per-pane border is the sole guard surface now.
    border = _line_starting("set -g pane-border-format ")
    assert "@GUARD" in border, "pane border must render the per-pane @GUARD marker"


def test_keystroke_lock_any_key_binding_arms_first_keystroke_no_refresh() -> None:
    """The root-table any-key binding is the sole arming surface for the
    keystroke-anchored typing lock. It must:

      * stamp the per-pane @TYPING_LOCK_UNTIL on a real keystroke,
      * KEEP an existing future value (no refresh — "5 min since FIRST
        keystroke", not since LAST), re-arming only when absent/expired,
      * source "now" from #{client_activity} (zero-fork; set -F can't expand %s),
      * pass mouse events through (`send-keys -M`) WITHOUT arming (focus/click
        must never lock a pane), discriminated by a fresh non-empty #{mouse_x},
      * faithfully REPLAY the real keystroke with a bare `send-keys`.
    """
    line = _line_starting("    set -Fp @TYPING_LOCK_UNTIL ")
    # No-refresh keep-or-rearm: keep @TYPING_LOCK_UNTIL while it is >= now, else
    # rearm to client_activity + 300s.
    assert "#{?#{e|>=:#{@TYPING_LOCK_UNTIL},#{client_activity}}" in line
    assert "#{e|+:#{client_activity},300}" in line
    # The kept branch re-writes the SAME value (the proof of no-refresh).
    assert "},#{@TYPING_LOCK_UNTIL}," in line

    conf = CONF.read_text(encoding="utf-8")
    assert "bind -n Any {" in conf
    # Mouse keys are excluded from arming and passed straight through.
    assert "if -F '#{!=:#{mouse_x},}' {" in conf
    assert "send-keys -M" in conf
    # Faithful replay of the matched key: a bare `send-keys` with no args
    # (tmux re-sends the bound key). Distinct from the `send-keys -M` mouse line.
    stripped = [ln.strip() for ln in conf.splitlines()]
    assert "send-keys" in stripped, "the keep branch must replay the key with a bare send-keys"


def test_keystroke_lock_enter_clears_lock_and_submits() -> None:
    """Enter (and tmux's C-m resolution of Return) clears the pane lock and
    passes the key through to submit. No focus-based clearing exists."""
    for key in ("Enter", "C-m"):
        line = _line_starting(f"bind -n {key} ")
        assert "set -pu @TYPING_LOCK_UNTIL" in line, f"{key} must UNSET the pane lock"
        assert "send-keys" in line, f"{key} must still pass through (submit)"


def test_portable_status_guard_indicator_is_also_per_pane() -> None:
    portable = (ROOT / "tmux" / "tmux-portable-status.conf").read_text(encoding="utf-8")
    status_right = next(
        line for line in portable.splitlines() if line.startswith("set status-right ")
    )
    assert "tmux-typing-guard-status --scan" in status_right
    assert "#(tmux-typing-guard-status 2>/dev/null)" not in status_right
