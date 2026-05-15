from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import hashlib
from pathlib import Path


class TmuxError(RuntimeError):
    """Raised when a tmux command fails."""


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
    _, slot = target.rsplit(":", 1)
    if not slot or slot.isdigit():
        return False
    return bool(_SLOT_RE.match(slot))


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


class TmuxAdapter:
    """Small wrapper around raw tmux commands.

    The adapter is the lowest Python tmux boundary. Pane-scoped target flags are
    resolved through tmuxctl before subprocess execution, so callers can pass
    stable custom ids (``1:N``, ``1:NW``, ``palace:N``, ``legion:custodes``) in
    place of volatile ``%N`` pane ids.
    """

    def __init__(self, tmux_binary: str | None = None) -> None:
        self.tmux_binary = tmux_binary or _tmux_binary()
        self._resolving_targets = False

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

    def run(self, *args: str, allow_failure: bool = False) -> str:
        resolved_args = self._resolve_tmux_args(tuple(args))
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

    def send_keys(self, target: str, *keys: str) -> None:
        self.run("send-keys", "-t", target, *keys)

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
        if submit_settle_seconds > 0:
            time.sleep(submit_settle_seconds)
        self.send_keys(target, "C-m")
        if submit_settle_seconds > 0:
            time.sleep(submit_settle_seconds)
        self.send_keys(target, "C-m")
