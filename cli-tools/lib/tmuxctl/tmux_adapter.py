from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import send_gate


class TmuxError(RuntimeError):
    """Raised when a tmux command fails."""


class TmuxSendGated(TmuxError):
    """Raised when the universal send gate suppressed a pane write.

    Distinct from a genuine tmux failure: NO bytes were written to the pane,
    so the caller may safely re-queue the write for delivery once the gate
    (quiet hours / typing guard) clears. Carries the structured gate result.
    """

    def __init__(self, gate: dict | None = None) -> None:
        self.gate = gate or {}
        reason = self.gate.get("reason", "gated")
        super().__init__(f"send suppressed by gate: {reason}")


DEFAULT_SUBMIT_SETTLE_SECONDS = 1.0
DEFAULT_PRE_SUBMIT_SETTLE_SECONDS = 1.0

_PANE_TARGET_COMMANDS = {
    "break-pane",
    "capture-pane",
    "display-message",
    "join-pane",
    "kill-pane",
    "move-pane",
    "pipe-pane",
    "respawn-pane",
    "resize-pane",
    "select-pane",
    "send-keys",
    "split-window",
    "swap-pane",
}
_PANE_OPTION_COMMANDS = {"set-option", "set", "show-options", "show"}
_PANE_TARGET_FLAGS = {"-t", "-s"}
_SLOT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_AUTOMATION_FOCUS_ENVS = {
    "IMPERIUM_TMUX_AUTOMATION",
    "TOKEN_API_INTERNAL_DISPATCH",
}
RUNTIME_PANE_OPTIONS = (
    "@INSTANCE_ID",
    "@CC_STATE",
    "@PANE_LABEL",
    "@ACTIVE_TITLE",
    "@PROGRESS_TITLE",
    "@PANE_PROGRESS",
    "@PANE_TITLE_SUPPRESS",
    "@TTS_STATE",
    "@CONTEXT_INFO",
    "@STACK_PENDING",
    "@GT_FIRE",
    "@PLANNING_STATE",
    "@PLANNING_AGENT",
    "@DISCORD_VOICE_LOCK",
    "@DISCORD_VOICE_PROCESSING",
    "@TOKEN_API_WRAPPER_LAUNCH_ID",
    "@TOKEN_API_ENGINE",
    "@TOKEN_API_LAUNCHER",
    "@TOKEN_API_CWD",
    "@TOKEN_API_SESSION_ID",
    "@TOKEN_API_DISPATCH_TARGET",
    "@TOKEN_API_DISPATCH_WINDOW",
    "@TOKEN_API_DISPATCH_MODE",
    "@TOKEN_API_DISPATCH_SLOT",
    "@TOKEN_API_LAUNCH_MODE",
    "@TOKEN_API_TARGET_WORKING_DIR",
)
PANE_STYLE_OPTIONS = ("window-style", "window-active-style")


def normalize_prompt_payload(text: str) -> str:
    """Normalize a live-agent prompt payload before pane injection."""
    normalized = re.sub(r"[\r\n]+", " ", text).rstrip()
    if not normalized.strip():
        raise ValueError("prompt payload is empty after normalization")
    return normalized


def prompt_payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tmux_binary() -> str:
    """Return the real tmux binary, not the optional Imperium wrapper."""
    for env_name in ("IMPERIUM_TMUX_BIN", "REAL_TMUX", "TMUX_BIN"):
        candidate = os.environ.get(env_name)
        if candidate:
            return candidate

    wrapper = Path(__file__).resolve().parents[2] / "bin" / "tmux"
    for candidate in (
        shutil.which("tmux", mode=os.F_OK | os.X_OK, path=os.environ.get("PATH")) or "",
        "/opt/homebrew/bin/tmux",
        "/usr/local/bin/tmux",
        "/usr/bin/tmux",
        "/bin/tmux",
    ):
        if not candidate:
            continue
        try:
            if Path(candidate).resolve() == wrapper.resolve():
                continue
        except OSError:
            pass
        return candidate
    return "tmux"


def tmux_binary() -> str:
    """Public accessor for the resolved real tmux binary (never the shim).

    A thin, stable wrapper over ``_tmux_binary`` so out-of-package readers (e.g.
    the ``tmux-typing-guard-status`` diagnostic) can depend on a public name
    rather than the private resolver.
    """
    return _tmux_binary()


def _looks_like_custom_pane_target(target: str) -> bool:
    if not target or target.startswith("%"):
        return False
    if target in {"current", "!", "{last}", "{next}", "{previous}"}:
        return False
    if ":" not in target:
        return False
    left, slot = target.rsplit(":", 1)
    if not slot:
        return False
    if slot.isdigit():
        return left.isdigit()
    if not _SLOT_RE.match(slot):
        return False
    return left.isdigit() or left in {
        "palace",
        "somnium",
        "legion",
        "mechanicus",
        "reservists",
        "mars",
        "kreig",
        "tui",
    }


def _command_has_pane_option_scope(args: tuple[str, ...]) -> bool:
    if not args:
        return False
    command = args[0]
    if command in _PANE_TARGET_COMMANDS:
        return True
    if command not in _PANE_OPTION_COMMANDS:
        return False
    # tmux show-options/set-option only targets a pane when -p is supplied;
    # tmux permits clustered forms like -pv and -pu.
    return any(arg.startswith("-") and "p" in arg for arg in args[1:])


def _target_arg(args: list[str], flag: str = "-t") -> str:
    for idx, arg in enumerate(args):
        if arg == flag and idx + 1 < len(args):
            return args[idx + 1]
        if arg.startswith(flag) and arg != flag:
            return arg[len(flag) :]
    return ""


def _has_flag(args: list[str] | tuple[str, ...], flag: str) -> bool:
    return any(arg == flag or (arg.startswith(flag) and arg != flag) for arg in args[1:])


def _select_pane_title_only(args: list[str] | tuple[str, ...]) -> bool:
    """True only for select-pane calls whose sole pane-affecting action is -T."""
    if not any(arg == "-T" or (arg.startswith("-T") and arg != "-T") for arg in args[1:]):
        return False
    i = 1
    while i < len(args):
        arg = args[i]
        if arg in {"-t", "-T"}:
            i += 2
            continue
        if (arg.startswith("-t") and arg != "-t") or (arg.startswith("-T") and arg != "-T"):
            i += 1
            continue
        return False
    return True


def _camera_mutating(args: list[str] | tuple[str, ...]) -> bool:
    """Whether a resolved tmux command can move the client's camera.

    Mirrors the command classification in ``_mechanicus_focus_guard_blocks``
    but answers a different question: not "is this allowed", but "did the
    wrapped operation displace focus at all". ``preserve_focus`` restores only
    when this fired during the operation, so a human navigating mid-op is
    never snapped back to a stale snapshot.
    """
    if not args:
        return False
    command = args[0]
    if command == "select-pane":
        # `select-pane -T` only sets a title when it is the sole pane-affecting
        # action.  `select-pane -P` looks like style state, but tmux still selects
        # the target pane and clears native zoom when the target is not active.
        return not _select_pane_title_only(args)
    if command in {"select-window", "switch-client"}:
        return True
    if command in {"split-window", "new-window", "join-pane", "break-pane"}:
        return not _has_flag(args, "-d")
    if command in {"kill-pane", "kill-window"}:
        # Killing the active pane/window moves the camera to a neighbor.
        return True
    if command == "resize-pane":
        return _has_flag(args, "-Z")
    return False


def _tmux_stderr_target(*, allow_failure: bool):
    # allow_failure callers intentionally tolerate tmux errors and almost never
    # consume stderr. Do not spend a second pipe for stderr; the live mechanicus:new
    # failure hit EMFILE while creating an unnecessary err pipe. Discard tolerated
    # stderr instead of merging it into stdout, because structured tmux stdout is
    # parsed by list_clients/list_sessions/list_panes callers.
    return subprocess.DEVNULL if allow_failure else subprocess.PIPE


class TmuxAdapter:
    """Small wrapper around raw tmux commands.

    The adapter is the lowest Python tmux boundary. Pane-scoped target flags are
    resolved through tmuxctl before subprocess execution, so callers can pass
    stable custom ids (``1:N``, ``palace:N``, ``somnium:SE``, ``council:custodes``) in
    place of volatile ``%N`` pane ids.
    """

    def __init__(self, tmux_binary: str | None = None) -> None:
        self.tmux_binary = tmux_binary or _tmux_binary()
        self._resolving_targets = False
        # When set, custom pane-target resolution snapshots THIS session instead
        # of the ambient current_session_name(). The restart executor pins it to
        # the freshly rebuilt session for the resume loop, whose generic run()
        # interception path cannot otherwise carry an explicit session argument.
        self.pinned_resolution_session: str | None = None
        # Last structured suppression result from the universal send gate, for
        # callers/tests that want to inspect why a send was a silent no-op.
        self.last_send_gate_result: dict | None = None
        # Monotonic count of camera-mutating commands executed through run().
        # preserve_focus snapshots it to decide whether the wrapped operation
        # displaced the camera (restore) or the human moved it (cede).
        self.focus_mutation_count = 0

    def _resolve_pane_target_arg(self, target: str) -> str:
        if not _looks_like_custom_pane_target(target):
            return target
        if self._resolving_targets:
            return target
        self._resolving_targets = True
        try:
            from .resolver import resolve_pane

            return resolve_pane(self, target).pane_id
        finally:
            self._resolving_targets = False

    def _resolve_tmux_args(self, args: tuple[str, ...]) -> list[str]:
        if not _command_has_pane_option_scope(args):
            return list(args)
        resolved = list(args)
        idx = 1
        while idx < len(resolved) - 1:
            if resolved[idx] in _PANE_TARGET_FLAGS:
                resolved[idx + 1] = self._resolve_pane_target_arg(resolved[idx + 1])
                idx += 2
                continue
            idx += 1
        return resolved

    def _mechanicus_focus_guard_blocks(self, args: list[str]) -> bool:
        """Fail-closed: no focus move into mechanicus without an explicit short override."""
        if not args:
            return False
        if os.environ.get("IMPERIUM_TMUX_FOCUS_RESTORE") == "1":
            return False
        command = args[0]
        target = ""
        focus_command = False
        if command == "select-pane":
            # `select-pane -T` is title-only only by itself.  `select-pane -P` and
            # `select-pane -Z ... -T ...` select/zoom as side effects, so guard
            # them like ordinary select-pane calls.
            if _select_pane_title_only(args):
                return False
            target = _target_arg(args)
            if not target:
                return False
            focus_command = True
        elif command in {"select-window", "switch-client"}:
            target = _target_arg(args)
            if not target:
                return False
            focus_command = True
        elif command in {"split-window", "new-window"}:
            # Detached creation is safe; foreground creation in mechanicus changes camera.
            if _has_flag(args, "-d"):
                return False
            target = _target_arg(args)
            if not target:
                return False
            focus_command = True
        else:
            return False

        from .focus_guard import (
            log_blocked,
            maybe_open_override_from_env,
            override_active,
            target_is_mechanicus,
        )

        automation = any(os.environ.get(name) for name in _AUTOMATION_FOCUS_ENVS)
        mechanicus = target_is_mechanicus(self, target)
        if not mechanicus and not (automation and focus_command):
            return False
        if os.environ.get("IMPERIUM_ALLOW_TMUX_FOCUS") == "1":
            return False
        if mechanicus and (
            maybe_open_override_from_env(
                self, target=target, command=command, surface="tmux-adapter"
            )
            or override_active(self)
        ):
            return False
        log_blocked(self, target=target, command=command, surface="tmux-adapter", argv=args)
        return True

    def _send_gate_source(self) -> str:
        """Diagnostic provenance tag for the automated-activity marker (argv0 basename)."""
        try:
            return os.path.basename(sys.argv[0]) or "tmuxctl"
        except Exception:
            return "tmuxctl"

    def _run_raw_tmux(self, args: list[str], *, allow_failure: bool = True) -> str:
        """Run tmux without target resolution, send gate, or focus guard."""
        proc = subprocess.run(
            [self.tmux_binary, *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=_tmux_stderr_target(allow_failure=allow_failure),
            check=False,
        )
        if proc.returncode != 0 and not allow_failure:
            stderr = proc.stderr.strip()
            raise TmuxError(stderr or f"tmux {' '.join(args)} failed")
        return proc.stdout

    def clear_pane_style(self, target: str) -> None:
        """Clear pane tint/title overlays. Best-effort and camera-neutral.

        Do not use ``select-pane -P`` here.  It is named like a focus command,
        trips human-facing audits, and has been suspected in live focus
        rubber-banding.  Pane tint is just per-pane style state, so mutate the
        pane options directly through raw tmux instead.
        """
        if not target:
            return
        target = self._resolve_pane_target_arg(target)
        for option in PANE_STYLE_OPTIONS:
            self._run_raw_tmux(["set-option", "-pu", "-t", target, option])
        self._run_raw_tmux(["select-pane", "-t", target, "-T", ""])

    def clear_pane_tint(self, target: str) -> None:
        """Clear only pane background tint without touching the pane title."""
        if not target:
            return
        target = self._resolve_pane_target_arg(target)
        for option in PANE_STYLE_OPTIONS:
            self._run_raw_tmux(["set-option", "-pu", "-t", target, option])

    def set_pane_tint(self, target: str, bg: str) -> None:
        """Apply or clear a pane background tint without selecting the pane."""
        if not target:
            return
        target = self._resolve_pane_target_arg(target)
        if not bg or bg == "default":
            self.clear_pane_tint(target)
            return
        style = f"bg={bg}"
        for option in PANE_STYLE_OPTIONS:
            self._run_raw_tmux(["set-option", "-p", "-t", target, option, style])

    def clear_runtime_state(self, target: str) -> None:
        """Clear runtime stamps and pane close/assertion chrome together."""
        if not target:
            return
        self.clear_pane_style(target)
        for option in RUNTIME_PANE_OPTIONS:
            self._run_raw_tmux(["set-option", "-pu", "-t", target, option])

    def _target_for_invariant(self, resolved_args: list[str]) -> str:
        target = _target_arg(resolved_args)
        if target:
            return target
        try:
            return self._run_raw_tmux(["display-message", "-p", "#{pane_id}"]).strip()
        except Exception:
            return ""

    def _preflight_runtime_invariants(self, resolved_args: list[str]) -> None:
        if not resolved_args:
            return
        command = resolved_args[0]
        if command == "respawn-pane":
            target = self._target_for_invariant(resolved_args)
            if target:
                self.clear_runtime_state(target)
            return
        if command not in {"set-option", "set"}:
            return
        if not any(arg.startswith("-") and "p" in arg for arg in resolved_args[1:]):
            return
        if not any(arg.startswith("-") and "u" in arg for arg in resolved_args[1:]):
            return
        option = resolved_args[-1] if resolved_args else ""
        if option == "@INSTANCE_ID":
            target = self._target_for_invariant(resolved_args)
            if target:
                self.clear_pane_style(target)

    def run(self, *args: str, allow_failure: bool = False) -> str:
        # Universal send gate — the inescapable pane-write sentinel. Every send
        # to a pane that originates in Python (token-api interventions, the
        # tmuxctl CLI, enforcement, pane recovery) funnels through run(), so
        # gating here means automated bytes do not race direct operator input:
        # quiet hours cancel by default, typing guard delays by default, and
        # sanctioned direct-input sends pierce with audit. Reads pass through
        # untouched. Never raises.
        args_tuple = tuple(args)
        # Clear any prior suppression payload so an allowed send (which also
        # returns empty stdout) is never misread as suppressed by callers/tests.
        self.last_send_gate_result = None
        # Resolve Imperium canonical pane targets (mechanicus:N, council:custodes,
        # 1:N, …) to physical %pane ids BEFORE the gate evaluates. The gate's
        # keystroke-lock read shells out to
        # `tmux show-options -pqv -t <target> @TYPING_LOCK_UNTIL`; tmux only
        # understands physical ids and native session:window addresses, so a
        # canonical id silently mis-resolves, the lock reads as unset, and the
        # gate then MISSES a keystroke-locked pane and clobbers the human's live
        # draft (send-gate-attended-scoping-clobber). The shell shim already
        # resolves-then-gates (bin/tmux: resolve_target before send_gate_suppresses);
        # this aligns the Python clobber path so both languages gate on the same
        # physical id. Resolution is read-only (live tmux snapshot), so doing it
        # ahead of a possible suppression return is safe.
        resolved_args = self._resolve_tmux_args(args_tuple)
        gate = send_gate.evaluate(resolved_args)
        if gate is not None and gate.get("policy") == "delay":
            send_gate.record_suppression(gate)
            if send_gate.wait_for_gate_clear(resolved_args):
                gate = None
            else:
                gate = {**gate, "policy": "cancel", "suppressed": True, "delay_failed": True}
        if gate is not None:
            send_gate.record_suppression(gate)
            if gate.get("suppressed"):
                self.last_send_gate_result = gate
                return ""
        if self._mechanicus_focus_guard_blocks(resolved_args):
            return ""
        self._preflight_runtime_invariants(resolved_args)
        if _camera_mutating(resolved_args):
            self.focus_mutation_count += 1
        # Every send through run() is automated by construction (see this method's
        # docstring): stamp the target pane so compute_work_state discounts the
        # woken agent's reflex activity from productivity. Resolved args carry the
        # canonical %pane_id; recorded only after suppression/focus-guard returns
        # (a blocked send writes nothing to the pane) and before the keys land, so
        # the marker is committed before the agent's hooks can fire. Never raises.
        send_gate.register_automated_send(resolved_args, source=self._send_gate_source())
        try:
            proc = subprocess.run(
                [self.tmux_binary, *resolved_args],
                text=True,
                stdout=subprocess.PIPE,
                stderr=_tmux_stderr_target(allow_failure=allow_failure),
                check=False,
            )
        except OSError as exc:
            if exc.errno == errno.EMFILE:
                raise TmuxError(
                    "tmux subprocess failed: too many open files while running "
                    f"tmux {' '.join(resolved_args)}"
                ) from exc
            raise
        if proc.returncode != 0 and not allow_failure:
            stderr = proc.stderr.strip()
            raise TmuxError(stderr or f"tmux {' '.join(resolved_args)} failed")
        return proc.stdout

    def has_session(self, session_name: str) -> bool:
        proc = subprocess.run(
            [self.tmux_binary, "has-session", "-t", session_name],
            text=True,
            capture_output=True,
            check=False,
        )
        return proc.returncode == 0

    def current_session_name(self) -> str:
        return self.run("display-message", "-p", "#{session_name}").strip()

    @contextlib.contextmanager
    def pin_resolution_session(self, session_name: str):
        """Pin custom-target resolution to an explicit session for the duration.

        During ``tx restart`` the executor runs detached after parking clients
        into ``_stash`` and killing the old leader, so the target-less
        ``current_session_name()`` no longer returns the rebuilt session. Pinning
        routes every public-label resolution — including the generic ``run()``
        interception path (``display-message``/``capture-pane``/``send-keys`` on
        a label) that cannot take a session argument — at the freshly built
        session instead of the wrong ambient one. Restores the prior pin on exit.
        """
        previous = self.pinned_resolution_session
        self.pinned_resolution_session = session_name
        try:
            yield
        finally:
            self.pinned_resolution_session = previous

    def list_windows(self, session_name: str) -> list[dict[str, str]]:
        fmt = "#{session_name}\t#{window_index}\t#{window_name}"
        lines = self.run("list-windows", "-t", session_name, "-F", fmt).splitlines()
        windows: list[dict[str, str]] = []
        for line in lines:
            session, index, name = line.split("\t")
            windows.append({"session_name": session, "window_index": index, "window_name": name})
        return windows

    def list_panes(self, target: str) -> list[dict[str, str]]:
        fmt = "\t".join(
            [
                "#{pane_id}",
                "#{session_name}",
                "#{window_index}",
                "#{window_name}",
                "#{pane_index}",
                "#{pane_width}",
                "#{pane_height}",
                "#{pane_current_command}",
                "#{pane_tty}",
                "#{pane_active}",
                "#{pane_current_path}",
            ]
        )
        lines = self.run("list-panes", "-t", target, "-F", fmt).splitlines()
        panes: list[dict[str, str]] = []
        for line in lines:
            (
                pane_id,
                session_name,
                window_index,
                window_name,
                pane_index,
                width,
                height,
                current_command,
                tty,
                active,
                cwd,
            ) = line.split("\t")
            panes.append(
                {
                    "pane_id": pane_id,
                    "session_name": session_name,
                    "window_index": window_index,
                    "window_name": window_name,
                    "pane_index": pane_index,
                    "width": width,
                    "height": height,
                    "current_command": current_command,
                    "tty": tty,
                    "active": active,
                    "cwd": cwd,
                }
            )
        return panes

    def list_clients(self) -> list[dict[str, str]]:
        fmt = "\t".join(
            [
                "#{client_tty}",
                "#{session_name}",
                "#{client_name}",
                "#{window_index}",
                "#{window_name}",
            ]
        )
        lines = self.run("list-clients", "-F", fmt, allow_failure=True).splitlines()
        clients: list[dict[str, str]] = []
        for line in lines:
            client_tty, session_name, client_name, window_index, window_name = line.split("\t")
            clients.append(
                {
                    "client_tty": client_tty,
                    "session_name": session_name,
                    "client_name": client_name,
                    "window_index": window_index,
                    "window_name": window_name,
                }
            )
        return clients

    def list_sessions(self) -> list[dict[str, str]]:
        fmt = "\t".join(
            [
                "#{session_name}",
                "#{session_group}",
                "#{window_index}",
                "#{window_name}",
            ]
        )
        lines = self.run("list-sessions", "-F", fmt, allow_failure=True).splitlines()
        sessions: list[dict[str, str]] = []
        for line in lines:
            session_name, session_group, window_index, window_name = line.split("\t")
            sessions.append(
                {
                    "session_name": session_name,
                    "session_group": session_group,
                    "window_index": window_index,
                    "window_name": window_name,
                }
            )
        return sessions

    def show_window_option(self, target: str, option: str) -> str:
        return self.run(
            "show-options",
            "-wv",
            "-t",
            target,
            option,
            allow_failure=True,
        ).strip()

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.run(
            "show-options",
            "-pv",
            "-t",
            pane_id,
            option,
            allow_failure=True,
        ).strip()

    def capture_pane(self, pane_id: str, *, lines: int = 10) -> str:
        return self.run("capture-pane", "-t", pane_id, "-p", "-S", str(-lines), allow_failure=True)

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.run("send-keys", "-t", target, *keys, allow_failure=allow_failure)

    def send_text_then_submit(
        self,
        target: str,
        text: str,
        *,
        clear_prompt: bool = False,
        pre_submit_keys: tuple[str, ...] = (),
        submit_settle_seconds: float = DEFAULT_SUBMIT_SETTLE_SECONDS,
    ) -> None:
        """Inject literal text and submit robustly.

        2026-05-10 live Codex repro: a prompt left queued by immediate
        text+submit was submitted by a later standalone key: ``tmux send-keys
        -t %119 Enter`` submitted the queued prompt and ``tmux send-keys -t
        %124 C-m`` submitted the queued prompt. That means the recovery token is
        a second submit after the TUI has had time to ingest the queued text.

        Implementation: send literal text, send C-m once, wait, then send C-m
        again. If the first C-m submits normally, the second C-m lands on an
        empty prompt and is a no-op in Claude/Codex. If the first C-m was
        swallowed as a newline, the delayed second C-m submits the queued prompt.
        """
        payload = normalize_prompt_payload(text)
        if clear_prompt:
            self.send_keys(target, "C-u")
        self.run("send-keys", "-t", target, "-l", payload)
        # The literal payload is the byte-bearing send. If the universal gate
        # suppressed it (quiet hours / typing guard) run() wrote nothing and
        # left the structured result on last_send_gate_result. Abort the whole
        # submit atomically rather than firing a bare C-m at an empty prompt —
        # zero bytes issued means the caller can re-queue cleanly.
        gate = getattr(self, "last_send_gate_result", None)
        if gate and gate.get("suppressed"):
            raise TmuxSendGated(gate)
        if submit_settle_seconds > 0:
            time.sleep(submit_settle_seconds)
        for key in pre_submit_keys:
            self.send_keys(target, key)
        if pre_submit_keys and submit_settle_seconds > 0:
            time.sleep(submit_settle_seconds)
        self.send_keys(target, "C-m")
        if submit_settle_seconds > 0:
            time.sleep(submit_settle_seconds)
        self.send_keys(target, "C-m")
