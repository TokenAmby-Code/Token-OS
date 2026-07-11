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
    assert "tmuxctld-ping POST /grid-expand" in line
    assert "expand=1" in line
    assert "#{client_tty}" in line
    assert "tmux-run" not in line
    assert "tmux-grid-expand" not in line
    # A transport/handler failure surfaces a concise human message, never a raw
    # `tmuxctld-ping-/…` transport slug.
    assert "tmuxctld-ping-/grid-expand-failed" not in line
    assert "IMPERIUM_TMUX_RAW" not in line
    assert "display-message 'expand failed'" in line


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
    assert "#(" not in status_right, "typing guard status rendering must be zero-fork"
    # The per-pane border is the sole guard surface now, reading only daemon-derived
    # JSON projections.
    border = _line_starting("set -g pane-border-format ")
    assert "@TYPING_GUARD_UNTIL" in border
    assert "@TYPING_GUARD_MARKER" in border


def test_typing_guard_marker_is_live_deadline_computed() -> None:
    border = _line_starting("set -g pane-border-format ")
    assert "#{?#{e|>=:#{@TYPING_GUARD_UNTIL},%s}, #{@TYPING_GUARD_MARKER} ,}" in border
    assert "@TYPING_GUARD_JSON" not in border


def test_blue_nametag_uses_only_pane_label_while_context_stays_outside() -> None:
    """Only the blue nametag segment is protected; other context may render outside it."""
    status_left = _line_starting("set -g status-left ")
    assert "#S" in status_left
    assert "@PERSONA" in status_left

    border = _line_starting("set -g pane-border-format ")
    start = border.index("#{?@PANE_LABEL,")
    end = border.index("}#{?#{e|>=:#{@TYPING_GUARD_UNTIL},%s}", start) + 1
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
        "@TYPING_GUARD_UNTIL",
        "@TYPING_GUARD_MARKER",
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
    reachable by the root Any topology. Live HUMAN disables that topology;
    PENDING re-enables it so the next ordinary key is re-armed by the helper and
    the real key is replayed."""
    conf = CONF.read_text(encoding="utf-8")
    assert "bind -n Any {" in conf
    assert "tmuxctld-ping POST /typing-guard-state cmd=arm pane=#{q:pane_id} seconds=300" in conf
    assert "now=#{client_activity}" in conf
    assert "@TYPING_GUARD_KIND" in conf
    assert "@TYPING_GUARD_UNTIL" in conf
    assert "tmux-typing-guard-" not in conf
    assert "set -Fp @TYPING_GUARD" not in conf

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
    assert "tmuxctld-ping POST /typing-guard-state cmd=arm" in any_binding
    assert "@TYPING_GUARD_KIND" not in any_binding
    assert "@TYPING_GUARD_UNTIL" not in any_binding
    assert "@TYPING_GUARD_KIND},pending" not in any_binding, (
        "Any must not depend directly on pending; a follow-up keystroke after "
        "Backspace/Ctrl+C pending must run the arm endpoint and convert pending to human"
    )
    mouse_else_branch = any_binding.rsplit("send-keys", 1)[1]
    assert mouse_else_branch.strip(" \n\t{}") == "", (
        "the mouse (else) branch of Any must consume the event — no arm and no "
        "send-keys after the keystroke branch"
    )
    assert "bind -n FocusIn" not in conf
    assert "bind -n FocusOut" not in conf


def test_typing_guard_focus_hooks_only_rehydrate_topology() -> None:
    conf = CONF.read_text(encoding="utf-8")
    for prefix in (
        "set-hook -g client-attached[20] ",
        "set-hook -g after-select-pane[20] ",
        "set-hook -g after-select-window[20] ",
    ):
        line = _line_starting(prefix)
        assert "tmuxctld-ping POST /typing-guard-topology" in line
        assert "cmd=rehydrate" in line
        assert "/typing-guard-state" not in line
        for forbidden in ("cmd=arm", "cmd=pending", "cmd=hold", "cmd=release", "cmd=expire-pane"):
            assert forbidden not in line
    focus_hook_lines = [
        line
        for line in conf.splitlines()
        if line.startswith("set-hook -g after-select")
        or line.startswith("set-hook -g client-attached[20]")
    ]
    assert focus_hook_lines
    assert all("@TYPING_GUARD" not in line for line in focus_hook_lines)


def test_typing_guard_pending_keys_are_permanently_bound() -> None:
    """Hard infra lock: topology may toggle root Any only.

    Pending/control keys must stay permanently bound so Backspace/C-h/C-c/Enter
    cannot fall through to root Any and be misclassified as ordinary typing.
    """
    conf = CONF.read_text(encoding="utf-8")
    any_index = conf.index("bind -n Any {")
    for key, seconds in {
        "Enter": "5",
        "C-m": "5",
        "BSpace": "15",
        "C-h": "15",
        "C-c": "15",
    }.items():
        line = _line_starting(f"bind -n {key} ")
        assert conf.index(line) > any_index
        assert "tmuxctld-ping POST /typing-guard-state cmd=pending" in line
        assert f"seconds={seconds}" in line
        assert "send-keys" in line

    assert "unbind-key -n Enter" not in conf
    assert "unbind-key -n C-m" not in conf
    assert "unbind-key -n BSpace" not in conf
    assert "unbind-key -n C-h" not in conf
    assert "unbind-key -n C-c" not in conf


def test_pending_binding_templates_match_conf_byte_for_byte(monkeypatch) -> None:
    """Deploy-coherence invariant: the daemon's canonical PENDING_BINDINGS templates
    (what ``reconcile_pending_bindings`` re-sources onto a running server after a
    deploy) must match the conf lines exactly. If the conf changes but the Python
    template doesn't, the reconcile would re-source a STALE form — this guard keeps
    the re-source truthful. Mirrors the same in-sync contract ANY_BINDING carries."""
    monkeypatch.syspath_prepend(str(ROOT.parent / "tmuxctld" / "lib"))
    from tmuxctl import typing_guard_state as tg

    for key, text, _is_edit in tg.PENDING_BINDINGS:
        assert _line_starting(f"bind -n {key} ") == text, (
            f"{key} binding drifted between tmux-base.conf and PENDING_BINDINGS"
        )


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
        assert "tmuxctld-ping POST /typing-guard-state" not in line
        assert "@TYPING_GUARD" not in line
    assert "send-keys -M" not in _line_starting("bind -n MouseDown1Pane ")


def test_mouse_bindings_never_reach_the_green_agent_guard_state() -> None:
    """The green agent typing-guard state is set ONLY by the daemon around a
    verified send — it must be unreachable from any mouse event. A regression
    here would re-open the softlock class fixed by 13789e5. Asserts every mouse
    binding is keyboard/data-free of the guard."""
    conf = CONF.read_text(encoding="utf-8")

    # The whole conf must never bind the agent hold from a key/mouse table; it is
    # a daemon-only option (lib/tmuxctl/typing_guard_state.py + send_gate.py).
    for line in conf.splitlines():
        if line.lstrip().startswith("bind"):
            assert "cmd=hold" not in line, "no key/mouse binding may acquire the agent hold"

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
        # Never touches any typing-guard projection ...
        assert "@TYPING_GUARD" not in line
        # ... never arms/holds/pends the guard ...
        assert "tmuxctld-ping POST /typing-guard-state" not in line
        # ... and never replays via the stale mouse-target path that softlocked.
        assert "send-keys -M" not in line
        assert "mouse_x" not in line
        assert "mouse_any_flag" not in line

    # MouseDown1Pane is the minimal keyboard-free focus move, not a guard arm.
    assert "select-pane -t =" in _line_starting("bind -n MouseDown1Pane ")


def test_typing_guard_submit_backspace_and_ctrl_c_use_one_pending_helper() -> None:
    """Enter/C-m/Backspace/Ctrl+C variants use the same pending endpoint; only
    the timeout differs."""
    conf = CONF.read_text(encoding="utf-8")
    assert "⌨ PENDING" not in conf
    for key in ("Enter", "C-m"):
        line = _line_starting(f"bind -n {key} ")
        assert (
            "tmuxctld-ping POST /typing-guard-state cmd=pending pane=#{q:pane_id} seconds=5" in line
        )
        assert "send-keys" in line
    for key in ("BSpace", "C-h", "C-c"):
        line = _line_starting(f"bind -n {key} ")
        pending_helper = (
            "tmuxctld-ping POST /typing-guard-state cmd=pending pane=#{q:pane_id} seconds=15"
        )
        assert line.count(pending_helper) == 1
        assert "cmd=arm" not in line
        assert "@TYPING_GUARD_KIND" in line
        assert "@TYPING_GUARD_UNTIL" in line
        # Repeated Backspace/Ctrl+C while pending is the first branch and contains no endpoint call.
        pending_branch = line.split("} {")[0]
        assert "tmuxctld-ping POST /typing-guard-state" not in pending_branch


def test_typing_guard_bindings_swallow_nonzero_exit_so_tmux_never_flashes() -> None:
    """Every keystroke-guard ping must end with ``|| true``.

    ``>/dev/null 2>&1`` silences only the ping's OWN streams; a nonzero exit
    (daemon slow / mid-restart / curl fail) still makes tmux's own ``run-shell``
    flash ``'<cmd>' returned <N>`` onto the Emperor's status line. That is the
    2026-07-03 mid-typing flash — a regression of #559, which removed the
    ``|| … display-message …`` clause that had ALSO been forcing exit 0. The
    trailing ``|| true`` forces the shell command to exit 0 so nothing surfaces.
    """
    conf = CONF.read_text(encoding="utf-8")
    # The Any arm binding.
    any_binding = conf.split("bind -n Any {", 1)[1].split("}\nbind -n Enter", 1)[0]
    assert ">/dev/null 2>&1 || true" in any_binding, (
        "the Any arm ping must swallow its nonzero exit with || true"
    )
    # The permanently-bound submit/edit keys.
    for key in ("Enter", "C-m", "BSpace", "C-h", "C-c"):
        line = _line_starting(f"bind -n {key} ")
        assert "tmuxctld-ping POST /typing-guard-state" in line
        # Each ping in the line is exit-swallowed; no ping ends in a bare redirect.
        assert ">/dev/null 2>&1 || true" in line, (
            f"{key} guard ping must end with || true so a nonzero ping never flashes"
        )
        assert ">/dev/null 2>&1 ;" not in line and '>/dev/null 2>&1"' not in line, (
            f"{key} must not leave a ping silenced by redirect alone (still flashes)"
        )
    # The rogue token must not survive in any BINDING (a historical comment
    # documenting the removed form is fine, so scope this to bind lines).
    for line in conf.splitlines():
        if line.lstrip().startswith("bind"):
            assert "tmuxctld-ping-/typing-guard-state-failed" not in line


def test_status_right_renders_any_hooks_marker_zero_fork() -> None:
    """The GLOBAL statusline exposes the daemon-pushed @ANY_HOOKS topology marker
    so the guard-ON == all-keystroke-hooks-dropped state is visible at a glance.
    It reads a pushed @-var (ZERO fork) and must never shell out."""
    status_right = _line_starting("set -g status-right ")
    assert "@ANY_HOOKS" in status_right
    # Both states render: the salient dropped-hooks state and the normal state.
    assert "Any:off" in status_right
    assert "Any:on" in status_right
    # Zero-fork: no #() shell-out, no display-message, no guard scan.
    assert "#(" not in status_right, "status-right must not shell out"
    assert "display-message" not in status_right
    # It gates on the @ANY_HOOKS value, not a per-pane guard projection.
    assert "#{==:#{@ANY_HOOKS},off}" in status_right
    # The timer segment is preserved alongside the new marker.
    assert "#{@TIMER_SEG}" in status_right


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
    assert "#{@PANE_LABEL} #[default],}#{?#{e|>=:#{@TYPING_GUARD_UNTIL},%s}," in border


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
    assert "#(" not in status_right


def test_prefix_e_hot_swaps_persona_engine() -> None:
    line = _line_starting("bind E ")
    assert "tmuxctld-ping POST /persona-engine" in line
    assert 'pane=\\"#{pane_id}\\"' in line
    assert "toggle=1" in line
    assert "tmux-run" not in line
    assert "tmuxctl persona-engine" not in line


def test_pane_died_hook_routes_to_tmuxctld_event_not_raw_respawn() -> None:
    line = _line_starting("set-hook -g pane-died[90] ")
    assert "tmuxctld-ping POST /event" in line
    assert "event=pane-died" in line
    assert "pane=#{pane_id}" in line
    assert "tmux-pane-respawn" not in line
    # Background control-plane hook: a failed ping must never flash a raw
    # diagnostic to the human status line. It silences both streams and forces
    # exit 0 so tmux's own run-shell never surfaces `'<cmd>' returned <N>` either.
    assert ">/dev/null 2>&1 || true" in line
    assert "display-message" not in line
    assert "tmuxctld-ping-/" not in line
    assert "IMPERIUM_TMUX_RAW" not in line


def test_first_slice_keybinds_route_to_tmuxctld_ping_not_tmux_run() -> None:
    """Only routes with implemented daemon handlers move off tmux-run."""

    expected = {
        "bind -T pane-select Enter ": ("/grid-expand", ('client=\\"#{client_tty}\\"', "expand=1")),
        "bind F ": ("/focus", ('window=\\"#{session_name}:#{window_index}\\"', "mode=toggle")),
        "bind e ": ("/grid-expand", ('client=\\"#{client_tty}\\"',)),
        "bind E ": ("/persona-engine", ('pane=\\"#{pane_id}\\"', "toggle=1")),
        "bind M ": ("/mode-toggle", ('pane=\\"#{pane_id}\\"',)),
        "bind S ": ("/open-session-doc", ('pane=\\"#{pane_id}\\"',)),
        "bind g ": ("/goto-spoken", ()),
    }

    for prefix, (path, payload_parts) in expected.items():
        line = _line_starting(prefix)
        assert f"tmuxctld-ping POST {path}" in line
        for payload in payload_parts:
            assert payload in line
        assert "tmux-run" not in line


def test_prefix_n_rename_routes_to_daemon_without_raw_slug() -> None:
    """prefix+n rename routes to the implemented daemon /pane-rename handler and
    passes the typed name (empty = interview). A ping failure is swallowed
    silently — it must never flash a raw `tmuxctld-ping-/pane-rename-failed`
    transport slug at the human.

    (The earlier draft of this guard asserted a not-yet-built explicit-name
    hybrid that shelled out to tmux-pane-rename AND kept the raw slug; that
    design was never implemented and its raw-slug requirement conflicts with the
    no-raw-status-line contract, so the guard now encodes the shipped daemon path.)
    """
    line = _line_starting("bind n ")
    assert "command-prompt" in line
    assert "tmuxctld-ping POST /pane-rename" in line
    assert 'pane=\\"#{pane_id}\\"' in line
    assert 'name=\\"%%\\"' in line
    assert ">/dev/null 2>&1 || true" in line
    assert "tmuxctld-ping-/pane-rename-failed" not in line
    assert "IMPERIUM_TMUX_RAW" not in line


def test_missing_daemon_endpoint_keybinds_remain_on_legacy_paths() -> None:
    """501-anchor keybinds must not route through tmuxctld-ping.

    The daemon /shuttle, /reset, /tts/listen routes are intentional 501 anchors
    and their legacy scripts are 410-on-touch shims (or, for tts-listen, absent),
    so there is no working handler to bind. Rather than flash a raw
    `tmuxctld-ping-/…-failed` transport slug (or a shim's `returned 154`), those
    keys are DISABLED with a concise, non-raw human message. prefix+B is the one
    genuinely-working legacy path — it binds directly to the real ethereal-prompt
    script — and Space/Q remain on their working script launchers.
    """
    # Disabled 501-anchor keys: concise human message, no daemon ping, no slug.
    for prefix, label in {
        "bind d ": "shuttle",
        "bind R ": "reset",
        "bind P ": "tts-listen",
    }.items():
        line = _line_starting(prefix)
        assert "display-message" in line
        assert label in line
        assert "tmuxctld-ping" not in line
        assert "tmuxctld-ping-/" not in line
        assert "IMPERIUM_TMUX_RAW" not in line

    # prefix+B: restored to the real, working ethereal-prompt script (not the
    # 501 daemon anchor). A script failure shows a concise non-raw message.
    bind_b = _line_starting("bind B ")
    assert "ethereal-prompt" in bind_b
    assert "tmuxctld-ping POST" not in bind_b
    assert "tmuxctld-ping-/" not in bind_b
    assert "IMPERIUM_TMUX_RAW" not in bind_b

    # Working script launchers stay off the daemon path.
    for prefix, command in {
        "bind Space ": "tmux-legion-prompt-popup",
        "bind Q ": "tmux-mark-for-close",
    }.items():
        line = _line_starting(prefix)
        assert command in line
        assert "tmuxctld-ping POST" not in line


def test_501_anchor_paths_are_not_bound_through_tmuxctld_ping() -> None:
    conf = CONF.read_text(encoding="utf-8")
    for route in (
        "/shuttle",
        "/mark-for-close",
        "/reset",
        "/ethereal-prompt",
        "/tts/listen",
        "/legion-prompt",
    ):
        assert f"tmuxctld-ping POST {route}" not in conf


def test_no_raw_transport_slug_flashes_to_status_line() -> None:
    """Anti-regression sweep: no live binding or hook may leak a raw
    `tmuxctld-ping-/…-failed` transport slug (or its IMPERIUM_TMUX_RAW marker)
    onto the human tmux status line. Comments documenting the removed pattern are
    allowed; only executable (non-comment) lines are checked.

    This is the core contract for the 2026-07-03 status-line-flash bug: 501-anchor
    keys are disabled with concise messages, background hooks fail silently, and
    implemented-route failures use concise human messages — none surface the raw
    internal transport token.
    """
    conf = CONF.read_text(encoding="utf-8")
    offenders = []
    for line in conf.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if "tmuxctld-ping-/" in line or "IMPERIUM_TMUX_RAW" in line:
            offenders.append(line)
    assert not offenders, "raw transport slug leaks to status line:\n" + "\n".join(offenders)


def test_lifecycle_hooks_fail_silently_without_raw_tokens() -> None:
    """pane-died / client-attached / client-detached are best-effort background
    control-plane notifications: a failed ping must silence both streams and
    force exit 0 (`>/dev/null 2>&1 || true`) so neither a raw slug nor tmux's own
    `'<cmd>' returned <N>` ever flashes at the human."""
    for prefix in (
        "set-hook -g pane-died[90] ",
        "set-hook -g client-attached[10] ",
        "set-hook -g client-detached ",
    ):
        line = _line_starting(prefix)
        assert "tmuxctld-ping POST" in line
        assert ">/dev/null 2>&1 || true" in line
        assert "display-message" not in line
        assert "tmuxctld-ping-/" not in line
        assert "IMPERIUM_TMUX_RAW" not in line


def test_implemented_route_keybinds_use_concise_message_not_raw_slug() -> None:
    """User-invoked implemented daemon routes still ping the daemon, but a
    transport/handler failure surfaces a concise human message — never a raw
    `tmuxctld-ping-/…` transport slug."""
    expected = {
        "bind F ": ("/focus", "focus failed"),
        "bind e ": ("/grid-expand", "expand failed"),
        "bind E ": ("/persona-engine", "persona swap failed"),
        "bind M ": ("/mode-toggle", "mode toggle failed"),
        "bind S ": ("/open-session-doc", "session doc unavailable"),
        "bind g ": ("/goto-spoken", "goto-spoken failed"),
    }
    for prefix, (route, message) in expected.items():
        line = _line_starting(prefix)
        assert f"tmuxctld-ping POST {route}" in line
        assert f"display-message '{message}'" in line
        assert "tmuxctld-ping-/" not in line
        assert "IMPERIUM_TMUX_RAW" not in line
        # Failure path exits 0 (via the display-message) so tmux never flashes
        # `'<cmd>' returned <N>` either; both ping streams are silenced first.
        assert ">/dev/null 2>&1 ||" in line


def test_workspace_launcher_remains_on_tmux_run_and_is_documented() -> None:
    conf = CONF.read_text(encoding="utf-8")
    line = _line_starting("bind W ")
    assert line == 'bind W run-shell "tmux-run tx start"'
    assert "# Workspace launcher. Deliberately left as the shell CLI" in conf
    assert "out of scope for daemon keybind rebinding" in conf


def _any_binding_block() -> str:
    conf = CONF.read_text(encoding="utf-8")
    return conf.split("bind -n Any {", 1)[1].split("}\nbind -n Enter", 1)[0]


def test_typing_guard_keystroke_bindings_never_flash_display_message() -> None:
    """Emperor ruling 2026-07-02: the typing-guard keystroke hooks must fail
    SILENTLY. A best-effort background arm/pending write must never surface the
    `tmuxctld-ping-/typing-guard-state-failed` status flash into the Emperor's
    client while he is typing (the rogue-error symptom)."""
    any_binding = _any_binding_block()
    assert "display-message" not in any_binding
    assert "tmuxctld-ping-/typing-guard-state-failed" not in any_binding
    for key in ("Enter", "C-m", "BSpace", "C-h", "C-c"):
        line = _line_starting(f"bind -n {key} ")
        assert "display-message" not in line, f"{key} must not flash a status message"
        assert "tmuxctld-ping-/typing-guard-state-failed" not in line
        # Silent redirect, not the noisy `|| tmux display-message` fallback.
        assert ">/dev/null 2>&1" in line
        assert "|| env IMPERIUM_TMUX_RAW=1 tmux display-message" not in line


def test_typing_guard_any_key_self_unbinds_before_arming() -> None:
    """The Any keystroke branch DROPS the hook synchronously (`unbind-key -n Any`)
    before firing the arm, so a single keystroke arms once and every later
    keystroke of the same message passes straight through — no per-keystroke ping
    storm even when the daemon is slow/unreachable."""
    any_binding = _any_binding_block()
    assert "unbind-key -n Any" in any_binding
    # Drop the hook FIRST, then arm.
    assert any_binding.index("unbind-key -n Any") < any_binding.index(
        "tmuxctld-ping POST /typing-guard-state cmd=arm"
    )
    # Arm failure is silent — no `||` display-message fallback survives.
    assert ">/dev/null 2>&1" in any_binding
    assert "display-message" not in any_binding


def test_typing_guard_edit_keys_pass_through_while_human_guard_is_live() -> None:
    """BSpace/C-h/C-c must NOT ping the daemon while a HUMAN (or PENDING) guard is
    already live — zero keystroke hook execution during active typing. The
    guard-live branch is a bare send-keys with no endpoint call."""
    for key in ("BSpace", "C-h", "C-c"):
        line = _line_starting(f"bind -n {key} ")
        # The short-circuit condition covers human as well as pending now.
        assert "#{==:#{@TYPING_GUARD_KIND},human}" in line
        assert "#{==:#{@TYPING_GUARD_KIND},pending}" in line
        guard_live_branch = line.split("} {")[0]
        assert "tmuxctld-ping" not in guard_live_branch
        assert "send-keys" in guard_live_branch
