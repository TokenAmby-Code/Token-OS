"""Assert-custodes: tmuxctl owns the detect→upsert/restart decision for the
legion:custodes orchestrator pane.

Callers (token-api state hook, morning_session launcher, any future trigger)
should only know "I want to deliver this prompt to Custodes." This module
decides:

- If the legion:custodes pane has a live `claude` process → upsert the prompt
  via `agent-cmd --pane`.
- Otherwise (shell, empty, dead) → fresh launch via `dispatch --persona
  custodes --pane <id> --prompt-file <path> --sync` (the non-deprecated
  replacement for `primarch custodes`).

The pane is resolved through the `@PANE_ID = legion:custodes` tag — the
canonical identity signal. If no pane carries the tag yet, the legion stack
is created via `add_orchestrator_stack_pane`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .resolver import resolve_pane
from .stack import CUSTODES_ROLE, add_orchestrator_stack_pane
from .tmux_adapter import TmuxAdapter

DISPATCH_BIN = "dispatch"
CLAUDE_CMD_BIN = "agent-cmd"

CLAUDE_PROCESS_NEEDLES = ("claude",)
AGENT_PROCESS_NEEDLES = ("claude", "codex")


def _ensure_custodes_pane(adapter: TmuxAdapter, session: str) -> str:
    try:
        return resolve_pane(adapter, CUSTODES_ROLE).pane_id
    except ValueError:
        pass
    add_orchestrator_stack_pane(adapter, session, "legion")
    return resolve_pane(adapter, CUSTODES_ROLE).pane_id


def _pane_pid(adapter: TmuxAdapter, pane_id: str) -> int | None:
    raw = adapter.run(
        "display-message",
        "-t",
        pane_id,
        "-p",
        "#{pane_pid}",
        allow_failure=True,
    ).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _process_tree() -> tuple[dict[int, list[int]], dict[int, str]]:
    """Return (children_by_ppid, command_by_pid) snapshot via `ps -A`."""
    try:
        proc = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}, {}
    if proc.returncode != 0:
        return {}, {}
    children: dict[int, list[int]] = {}
    commands: dict[int, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        commands[pid] = parts[2].lower()
        children.setdefault(ppid, []).append(pid)
    return children, commands


def _pane_has_active_process(pane_pid: int | None, needles: tuple[str, ...]) -> bool:
    """True if any descendant of pane_pid command contains one of needles."""
    if not pane_pid:
        return False
    children, commands = _process_tree()
    if not commands:
        return False
    stack = list(children.get(pane_pid, []))
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        command = commands.get(pid, "")
        if any(needle in command for needle in needles):
            return True
        stack.extend(children.get(pid, []))
    return False


def pane_has_active_claude(pane_pid: int | None) -> bool:
    """True if any descendant of pane_pid is a live claude process.

    tmux's ``#{pane_current_command}`` returns the foreground process group
    leader at the TTY — for panes launched via ``claude-wrapper.sh`` that is
    bash, not claude. Walking the process tree is the canonical signal.
    """
    return _pane_has_active_process(pane_pid, CLAUDE_PROCESS_NEEDLES)


def pane_has_active_agent(pane_pid: int | None) -> bool:
    """True if any descendant of pane_pid is a live Claude or Codex process.

    ``assert-instance`` is engine-neutral: a live Codex pane is a valid managed
    runtime and must not be pruned/stopped just because the detector was written
    originally for Claude-only persona panes.
    """
    return _pane_has_active_process(pane_pid, AGENT_PROCESS_NEEDLES)


def _upsert_via_claude_cmd(pane_id: str, prompt: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [CLAUDE_CMD_BIN, "--pane", pane_id, prompt],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError as exc:
        return False, f"agent-cmd not found: {exc}"
    except subprocess.TimeoutExpired:
        return False, "agent-cmd timed out"
    if proc.returncode != 0:
        return False, f"agent-cmd rc={proc.returncode}: {proc.stderr.strip()[:200]}"
    return True, "ok"


def _launch_via_dispatch(pane_id: str, prompt_file: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [
                DISPATCH_BIN,
                "--persona",
                "custodes",
                "--pane",
                pane_id,
                "--prompt-file",
                str(prompt_file),
                "--sync",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        return False, f"dispatch not found: {exc}"
    except subprocess.TimeoutExpired:
        return False, "dispatch timed out"
    if proc.returncode != 0:
        return False, f"dispatch rc={proc.returncode}: {proc.stderr.strip()[:200]}"
    return True, "ok"


def assert_custodes(
    adapter: TmuxAdapter,
    prompt: str,
    *,
    session: str = "main",
) -> dict[str, Any]:
    """Deliver `prompt` to the legion:custodes pane, upserting or launching."""
    pane_id = _ensure_custodes_pane(adapter, session)
    pane_pid = _pane_pid(adapter, pane_id)

    if pane_has_active_claude(pane_pid):
        ok, reason = _upsert_via_claude_cmd(pane_id, prompt)
        return {
            "dispatched": ok,
            "reason": "upserted_existing_pane" if ok else f"upsert_failed: {reason}",
            "tmux_pane": pane_id,
            "pane_pid": pane_pid,
        }

    fd, path_str = tempfile.mkstemp(prefix="custodes-assert-", suffix=".md")
    os.close(fd)
    prompt_file = Path(path_str)
    try:
        prompt_file.write_text(prompt)
        ok, reason = _launch_via_dispatch(pane_id, prompt_file)
    finally:
        try:
            prompt_file.unlink()
        except FileNotFoundError:
            pass
    return {
        "dispatched": ok,
        "reason": "launched_new_custodes" if ok else f"launch_failed: {reason}",
        "tmux_pane": pane_id,
        "pane_pid": pane_pid,
    }
