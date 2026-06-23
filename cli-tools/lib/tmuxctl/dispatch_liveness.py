"""Dispatch launch-liveness probes — the engine-agnostic success criterion.

``dispatch`` must declare a launch a success when **the pane came up and a live
agent process is running in it**, NOT when a DB ``instances`` row binds within a
fixed window. The row stamp lags (cold start under fleet load) and for codex may
never land at all (the singleton-undercount path) — gating success on it produced
the fleet-wide exit-70 false-failure that misreported live launches and, on a
blind retry, stacked a SECOND agent into one git worktree.

This module answers the two liveness questions ``dispatch`` actually needs, both
read from the **process tree** (the same battle-tested walk that gates
close-lifecycle reaps via :func:`tmuxctl.liveness.detect_pane_tui`) so the verdict
is identical for Claude and Codex:

* :func:`pane_is_live` — does THIS pane have a live Claude/Codex agent right now?
  (the launch success criterion)
* :func:`live_agents_in_dir` — which panes are running a live agent rooted in a
  given working directory? (the duplicate-refusal guard: two agents racing one
  worktree is the corruption we fail safe against)

Read-only: a tmux snapshot + ``ps``. No DB, no pane mutation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .liveness import detect_pane_tui
from .tmux_adapter import TmuxAdapter


def pane_is_live(adapter: TmuxAdapter, pane_id: str) -> bool:
    """True iff a live Claude/Codex agent process is running under ``pane_id``."""
    if not pane_id:
        return False
    return detect_pane_tui(adapter, pane_id).live


@dataclass(frozen=True)
class LiveAgentPane:
    """A pane found running a live agent, with the cwd it is rooted in."""

    pane_id: str
    pane_pid: int | None
    agent_pid: int | None
    agent_command: str | None
    cwd: str


def _normalize(path: str) -> str:
    """Resolve a path for comparison; fail open to the raw value."""
    if not path:
        return ""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _all_panes(adapter: TmuxAdapter) -> list[tuple[str, str]]:
    """(pane_id, cwd) for every pane in every session. Empty on a dead server."""
    raw = adapter.run(
        "list-panes",
        "-a",
        "-F",
        "#{pane_id}\t#{pane_current_path}",
        allow_failure=True,
    ).strip()
    panes: list[tuple[str, str]] = []
    if not raw:
        return panes
    for line in raw.splitlines():
        if "\t" in line:
            pane_id, cwd = line.split("\t", 1)
        else:
            pane_id, cwd = line, ""
        if pane_id:
            panes.append((pane_id, cwd))
    return panes


def live_agents_in_dir(
    adapter: TmuxAdapter,
    work_dir: str,
    *,
    exclude_pane: str | None = None,
) -> list[LiveAgentPane]:
    """Live agent panes whose pane cwd resolves to ``work_dir``.

    ``exclude_pane`` drops one pane (the dispatcher's own pane) so a launch is not
    refused by the launching agent's own liveness. Detection is by the process
    tree, so a row-less / undercounted live agent — exactly the duplicate FG hit —
    is still caught.
    """
    target = _normalize(work_dir)
    if not target:
        return []
    found: list[LiveAgentPane] = []
    for pane_id, cwd in _all_panes(adapter):
        if exclude_pane and pane_id == exclude_pane:
            continue
        if _normalize(cwd) != target:
            continue
        tui = detect_pane_tui(adapter, pane_id)
        if not tui.live:
            continue
        found.append(
            LiveAgentPane(
                pane_id=pane_id,
                pane_pid=tui.pane_pid,
                agent_pid=tui.agent_pid,
                agent_command=tui.agent_command,
                cwd=cwd,
            )
        )
    return found
