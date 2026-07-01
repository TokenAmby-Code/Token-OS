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
    guard state per-pane. status-right must not run the old silent --scan
    reconciler; pane-border liveness must come from live deadline math.
    """
    status_right = _line_starting("set -g status-right ")
    assert "tmux-typing-guard-status" not in status_right
    assert "#(" not in status_right, "typing guard status rendering must be zero-fork"
    # The per-pane border is the sole guard surface now, but it must compute
    # liveness from deadlines instead of trusting stored @GUARD projection.
    border = _line_starting("set -g pane-border-format ")
    assert "@TYPING_LOCK_UNTIL" in border
    assert "@TYPING_PENDING_UNTIL" in border
    assert "@GUARD" not in border, "stored @GUARD must not drive live marker rendering"


def test_typing_guard_marker_is_live_deadline_computed() -> None:
    border = _line_starting("set -g pane-border-format ")
    assert "#{?#{e|>=:#{@TYPING_PENDING_UNTIL},%s}, #[fg=red]#[bold]⌨#[default] ," in border
    assert "#{?#{e|>=:#{@TYPING_LOCK_UNTIL},%s}, #[fg=colour214]#[bold]⌨#[default] ,}" in border
    assert "#{?@GUARD," not in border


def test_blue_nametag_uses_only_pane_label_while_context_stays_outside() -> None:
    """Only the blue nametag segment is protected; other context may render outside it."""
    status_left = _line_starting("set -g status-left ")
    assert "#S" in status_left
    assert "@PERSONA" in status_left

    border = _line_starting("set -g pane-border-format ")
    start = border.index("#{?@PANE_LABEL,")
    end = border.index("}#{?#{e|>=:#{@TYPING_PENDING_UNTIL},%s}", start) + 1
    blue_nametag = border[start:end]

    assert "#[bg=colour31]" in blue_nametag
    assert "#{@PANE_LABEL}" in blue_nametag
    for forbidden in (
        "@PERSONA",
        "@SESSION_DOC",
        "@CWD",
        "pane_title",
        "pane_current_path",
        "pane_current_command",
    ):
        assert forbidden not in blue_nametag

    assert "@PERSONA" not in border
    assert "pane_title" not in border
    assert "@PANE_TITLE_SUPPRESS" not in border
    for expected in (
        "@TYPING_LOCK_UNTIL",
        "@TYPING_PENDING_UNTIL",
        "@OPS_SELECTED",
        "@GT_FIRE",
        "@DISCORD_VOICE_PROCESSING",
        "@DISCORD_VOICE_LOCK",
        "@TTS_STATE",
        "@CC_STATE",
        "@SESSION_DOC",
        "@CWD",
    ):
        assert expected in border


def test_typing_guard_any_key_routes_first_arm_through_canonical_helper() -> None:
    """The root-table any-key binding only calls the state helper when the pane is
    not ON. Live ON is preserved; PENDING is re-armed by the helper and the real
    key is replayed."""
    conf = CONF.read_text(encoding="utf-8")
    assert "bind -n Any {" in conf
    assert "tmux-typing-guard-state arm --pane #{q:pane_id} --seconds 300" in conf
    assert "--now #{client_activity}" in conf
    assert "@TYPING_PENDING_UNTIL" in conf
    assert "@TYPING_LOCK_UNTIL" in conf
    assert "tmux-typing-guard-status --expire-pane" not in conf
    assert "tmux-typing-guard-status --scan" not in conf
    assert "set -p @GUARD" not in conf, "@GUARD writes must be centralized in the helper"
    assert "set -Fp @TYPING_PENDING_UNTIL" not in conf
    assert "set -Fp @TYPING_LOCK_UNTIL" not in conf

    # Root-table Any is the catch-all backstop for EVERY mouse event lacking a
    # more specific binding (border clicks, status clicks, double/triple/second
    # clicks, status-left/right, …). The explicit MouseUp/Drag no-ops below cover
    # only common cases and provably MISS others — which is how
    # "mouse arms the typing guard" regressed ~5x (each fix whack-a-moled a few
    # more event names while Any kept catching the rest). So Any itself must
    # discriminate: a real mouse event carries a non-empty #{mouse_x}; a
    # keystroke leaves it empty. Branch on emptiness — #{==:#{mouse_x},} is true
    # ONLY for a keystroke and, unlike the truthiness form #{?mouse_x,…},
    # classifies a column-0 click (mouse_x=="0") correctly as a mouse event.
    any_binding = conf.split("bind -n Any {", 1)[1].split("}\nbind -n Enter", 1)[0]
    assert "if -F '#{==:#{mouse_x},}'" in any_binding, (
        "Any must discriminate keystroke (mouse_x empty) from mouse event "
        "(mouse_x set) via an emptiness test — not the broken #{mouse_any_flag} "
        "app-mode flag, and not the col-0-unsafe #{?mouse_x,…} truthiness form"
    )
    # #{mouse_any_flag} is the pane APP's mouse-reporting mode, not a per-event
    # discriminator (it reads 0 for real click/release events); it must not gate
    # the arm. And a mouse event must never replay via -M (the "no mouse target"
    # softlock, cf. 13789e5) — the discriminator is read-only and the mouse
    # branch consumes without re-sending.
    assert "mouse_any_flag" not in any_binding
    assert "send-keys -M" not in any_binding
    # Arming lives ONLY on the keystroke side; the mouse (else) branch is empty —
    # nothing after the keystroke branch's final send-keys but closing braces.
    assert "tmux-typing-guard-state arm" in any_binding
    assert "@TYPING_PENDING_UNTIL" not in any_binding, (
        "Any must not depend directly on PENDING; a follow-up keystroke after "
        "Backspace/Ctrl+C pending must run the arm helper and convert PENDING to ON"
    )
    mouse_else_branch = any_binding.rsplit("send-keys", 1)[1]
    assert mouse_else_branch.strip(" \n\t{}") == "", (
        "the mouse (else) branch of Any must consume the event — no arm and no "
        "send-keys after the keystroke branch"
    )
    assert "bind -n FocusIn" not in conf
    assert "bind -n FocusOut" not in conf


def test_mouse_scroll_status_and_focus_bindings_never_arm_or_pending() -> None:
    for key in (
        "MouseDown1Status",
        "MouseDown1Pane",
        "MouseUp1Pane",
        "MouseDrag1Pane",
        "MouseDragEnd1Pane",
        "MouseDown2Pane",
        "MouseUp2Pane",
        "MouseDown3Pane",
        "MouseUp3Pane",
        "WheelUpPane",
        "WheelDownPane",
    ):
        line = _line_starting(f"bind -n {key} ")
        assert "tmux-typing-guard-state arm" not in line
        assert "tmux-typing-guard-state pending" not in line
        assert "@TYPING_LOCK_UNTIL" not in line
        assert "@TYPING_PENDING_UNTIL" not in line
    assert "send-keys -M" not in _line_starting("bind -n MouseDown1Pane ")


def test_mouse_bindings_never_reach_the_green_agent_guard_state() -> None:
    """The green `agent` typing-guard state (@TYPING_AGENT_UNTIL) is set ONLY by
    the daemon around a verified send — it must be unreachable from any mouse
    event. A regression here would re-open the softlock class fixed by 13789e5
    (a mouse branch arming/holding the guard, then `send-keys -M` with no mouse
    target). Asserts every mouse binding is keyboard/data-free of the guard."""
    conf = CONF.read_text(encoding="utf-8")

    # The whole conf must never bind the agent hold from a key/mouse table; it is
    # a daemon-only option (lib/tmuxctl/typing_guard_state.py + send_gate.py).
    for line in conf.splitlines():
        if line.lstrip().startswith("bind"):
            assert "@TYPING_AGENT_UNTIL" not in line, (
                "no key/mouse binding may set or read the green agent hold; "
                "@TYPING_AGENT_UNTIL is daemon-only"
            )

    mouse_keys = (
        "MouseDown1Status",
        "MouseDown1Pane",
        "MouseUp1Pane",
        "MouseDrag1Pane",
        "MouseDragEnd1Pane",
        "MouseDown2Pane",
        "MouseUp2Pane",
        "MouseDown3Pane",
        "MouseUp3Pane",
        "WheelUpPane",
        "WheelDownPane",
    )
    for key in mouse_keys:
        line = _line_starting(f"bind -n {key} ")
        # Never touches any typing-guard option (human OR agent) ...
        assert "@TYPING_AGENT_UNTIL" not in line
        assert "@TYPING_LOCK_UNTIL" not in line
        assert "@TYPING_PENDING_UNTIL" not in line
        # ... never arms/holds/pends the guard ...
        assert "tmux-typing-guard-state arm" not in line
        assert "tmux-typing-guard-state pending" not in line
        assert "tmux-typing-guard-state hold" not in line
        # ... and never replays via the stale mouse-target path that softlocked.
        assert "send-keys -M" not in line
        assert "mouse_x" not in line
        assert "mouse_any_flag" not in line

    # MouseDown1Pane is the minimal keyboard-free focus move, not a guard arm.
    assert "select-pane -t =" in _line_starting("bind -n MouseDown1Pane ")


def test_typing_guard_submit_backspace_and_ctrl_c_use_one_pending_helper() -> None:
    """Enter/C-m/Backspace/Ctrl+C variants use the same pending transition
    helper; only the timeout differs. No @GUARD value may contain literal
    PENDING text."""
    conf = CONF.read_text(encoding="utf-8")
    assert "⌨ PENDING" not in conf
    for key in ("Enter", "C-m"):
        line = _line_starting(f"bind -n {key} ")
        assert "tmux-typing-guard-state pending --pane #{q:pane_id} --seconds 5" in line
        assert "send-keys" in line
    for key in ("BSpace", "C-h", "C-c"):
        line = _line_starting(f"bind -n {key} ")
        assert "tmux-typing-guard-state pending --pane #{q:pane_id} --seconds 15" in line
        assert "@TYPING_PENDING_UNTIL" in line
        # Repeated Backspace/Ctrl+C while pending is the first branch and contains no helper call.
        pending_branch = line.split("} {")[0]
        assert "tmux-typing-guard-state" not in pending_branch


def test_pane_border_identity_is_blank_by_default() -> None:
    """A clean pane (no agent) shows NO nametag.

    Regression: the border used to fall through to `#{pane_title}` (the machine
    hostname) whenever no pane label was stamped, gated by a @PANE_TITLE_SUPPRESS
    flag that was never set. The fix makes blank the DEFAULT: the identity segment
    renders an agent tag ONLY when @PANE_LABEL is stamped, and renders empty
    otherwise — no persona/hostname fallback, no suppress flag.
    """
    border = _line_starting("set -g pane-border-format ")
    # Agent identity renders from the pane label only; persona is statusline-only.
    assert "#{?@PERSONA," not in border
    assert "#{@PERSONA}" not in border
    assert "#{?@PANE_LABEL," in border
    assert "#{@PANE_LABEL}" in border
    # The @PANE_LABEL false-branch is empty — neither the hostname fallback nor the
    # retired suppress flag may reappear.
    assert "#{pane_title}" not in border, "hostname #{pane_title} fallback must be gone"
    assert "@PANE_TITLE_SUPPRESS" not in border, "retired suppress flag must be gone"
    # Structural proof of blank-by-default: the @PANE_LABEL conditional closes with
    # an empty false-branch (`,}`) directly into the live typing-guard deadline segment.
    assert "#{@PANE_LABEL} #[default],}#{?#{e|>=:#{@TYPING_PENDING_UNTIL},%s}," in border


def test_pane_title_suppress_concept_is_fully_retired() -> None:
    """@PANE_TITLE_SUPPRESS must not survive anywhere in the tmux config: not in the
    border format, not in comments documenting the (now-removed) inverted default."""
    conf = CONF.read_text(encoding="utf-8")
    assert "@PANE_TITLE_SUPPRESS" not in conf


def test_portable_status_guard_indicator_is_also_per_pane() -> None:
    portable = (ROOT / "tmux" / "tmux-portable-status.conf").read_text(encoding="utf-8")
    status_right = next(
        line for line in portable.splitlines() if line.startswith("set status-right ")
    )
    assert "tmux-typing-guard-status" not in status_right
    assert "#(" not in status_right


def test_prefix_e_hot_swaps_persona_engine() -> None:
    line = _line_starting("bind E ")
    assert "tmuxctl persona-engine" in line
    assert "--pane '#{pane_id}'" in line
    assert "--toggle" in line


def test_pane_died_hook_routes_to_tmuxctld_event_not_raw_respawn() -> None:
    line = _line_starting("set-hook -g pane-died[90] ")
    assert "tmuxctld-ping POST /event" in line
    assert "event=pane-died" in line
    assert "pane=#{pane_id}" in line
    assert ">/dev/null" in line
    assert "display-message" in line
    assert "tmux-pane-respawn" not in line
