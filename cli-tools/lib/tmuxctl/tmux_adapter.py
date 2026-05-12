from __future__ import annotations

import subprocess
import time


class TmuxError(RuntimeError):
    """Raised when a tmux command fails."""


DEFAULT_SUBMIT_SETTLE_SECONDS = 0.3


class TmuxAdapter:
    """Small wrapper around raw tmux commands."""

    def run(self, *args: str, allow_failure: bool = False) -> str:
        proc = subprocess.run(
            ["tmux", *args],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0 and not allow_failure:
            stderr = proc.stderr.strip()
            raise TmuxError(stderr or f"tmux {' '.join(args)} failed")
        return proc.stdout

    def has_session(self, session_name: str) -> bool:
        proc = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
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
        if clear_prompt:
            self.send_keys(target, "C-u")
        self.run("send-keys", "-t", target, "-l", text)
        self.send_keys(target, "C-m")
        if submit_settle_seconds > 0:
            time.sleep(submit_settle_seconds)
            self.send_keys(target, "C-m")
