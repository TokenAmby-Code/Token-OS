from __future__ import annotations

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


def _has_flag(args: list[str], flag: str) -> bool:
    return any(arg == flag or (arg.startswith(flag) and arg != flag) for arg in args[1:])


class TmuxAdapter:
    """Small wrapper around raw tmux commands.

    The adapter is the lowest Python tmux boundary. Pane-scoped target flags are
    resolved through tmuxctl before subprocess execution, so callers can pass
    stable custom ids (``1:N``, ``palace:N``, ``somnium:SE``, ``legion:custodes``) in
    place of volatile ``%N`` pane ids.
    """

    def __init__(self, tmux_binary: str | None = None) -> None:
        self.tmux_binary = tmux_binary or _tmux_binary()
        self._resolving_targets = False
        # Last structured suppression result from the universal send gate, for
        # callers/tests that want to inspect why a send was a silent no-op.
        self.last_send_gate_result: dict | None = None

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
            # select-pane -P/-T changes pane style/title, not the active camera.
            if any(arg == "-P" or arg == "-T" for arg in args[1:]):
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

    def run(self, *args: str, allow_failure: bool = False) -> str:
        # Universal send gate — the inescapable pane-write sentinel. Every send
        # to a pane that originates in Python (token-api interventions, the
        # tmuxctl CLI, enforcement, pane recovery) funnels through run(), so
        # gating here means no byte reaches a pane while quiet hours OR the
        # typing guard is active. Reads pass through untouched; sanctioned
        # human-initiated sends are allowed but logged. Never raises.
        args_tuple = tuple(args)
        # Clear any prior suppression payload so an allowed send (which also
        # returns empty stdout) is never misread as suppressed by callers/tests.
        self.last_send_gate_result = None
        gate = send_gate.evaluate(args_tuple)
        if gate is not None:
            send_gate.record_suppression(gate)
            if gate.get("suppressed"):
                self.last_send_gate_result = gate
                return ""
        resolved_args = self._resolve_tmux_args(args_tuple)
        if self._mechanicus_focus_guard_blocks(resolved_args):
            return ""
        # Every send through run() is automated by construction (see this method's
        # docstring): stamp the target pane so compute_work_state discounts the
        # woken agent's reflex activity from productivity. Resolved args carry the
        # canonical %pane_id; recorded only after suppression/focus-guard returns
        # (a blocked send writes nothing to the pane) and before the keys land, so
        # the marker is committed before the agent's hooks can fire. Never raises.
        send_gate.register_automated_send(resolved_args, source=self._send_gate_source())
        proc = subprocess.run(
            [self.tmux_binary, *resolved_args],
            text=True,
            capture_output=True,
            check=False,
        )
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
        self.send_keys(target, "C-m")
        if submit_settle_seconds > 0:
            time.sleep(submit_settle_seconds)
        self.send_keys(target, "C-m")
