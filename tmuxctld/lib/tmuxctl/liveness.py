"""Close-lifecycle liveness guard: is a real agent TUI alive on a pane?

The reap/close lifecycle must never retire a DB instance whose Claude/Codex TUI
process is still running — that strands the live worker (registry says "gone",
reality says "still running") and is exactly how the #314 husk reap orphaned a
live pane. This module answers the one question the guard needs: *does this
pane (or this instance) have a live agent process right now?*

Liveness is read from the **process tree**, not from tmux's
``#{pane_current_command}`` (which reads the foreground TTY leader — ``bash``
for wrapper-launched agents) and not from a stamp/DB row (which churns). We
reuse :func:`tmuxctl.custodes.active_agent_in_pane`, the same battle-tested walk
that gates pane reuse in ``list_free_panes``. A bare idle shell or a
truly-dead pane has no Claude/Codex descendant and reads as *not live*, so
genuine hygiene reaps (the #314 husk) are never falsely refused.

Read-only: tmux snapshot + ``ps``. No DB, no pane mutation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .custodes import active_agent_in_pane
from .tmux_adapter import TmuxAdapter


@dataclass(frozen=True)
class LiveTui:
    """Liveness verdict for one pane.

    ``live`` is True iff a Claude/Codex process is running under ``pane_pid``;
    ``agent_pid``/``agent_command`` name it for the guard's refusal payload.
    """

    pane_id: str
    pane_pid: int | None
    agent_pid: int | None
    agent_command: str | None

    @property
    def live(self) -> bool:
        return self.agent_pid is not None


def read_pane_pid(adapter: TmuxAdapter, pane_id: str) -> int | None:
    """The pane's shell pid (``#{pane_pid}``), or None for a vanished pane."""
    if not pane_id:
        return None
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


def detect_pane_tui(adapter: TmuxAdapter, pane_id: str) -> LiveTui:
    """Walk one pane's process subtree for a live Claude/Codex TUI."""
    pane_pid = read_pane_pid(adapter, pane_id)
    agent = active_agent_in_pane(pane_pid)
    if agent is None:
        return LiveTui(pane_id=pane_id, pane_pid=pane_pid, agent_pid=None, agent_command=None)
    agent_pid, command = agent
    return LiveTui(
        pane_id=pane_id,
        pane_pid=pane_pid,
        agent_pid=agent_pid,
        agent_command=command,
    )


def _stamped_panes(adapter: TmuxAdapter, instance_id: str) -> list[tuple[str, int | None]]:
    """Every live pane carrying ``@INSTANCE_ID == instance_id`` as (pane_id, pid).

    A single global ``list-panes -a`` scan. This is the divergence safety net:
    when the registry resolves an instance to a *stale* pane handle (the #314
    failure: ``close`` reported ``already_closed`` while the real TUI ran on
    another physical pane), the live pane still carrying the stamp is found here
    so the guard can refuse rather than retire-and-orphan.
    """
    raw = adapter.run(
        "list-panes",
        "-a",
        "-F",
        "\t".join(["#{pane_id}", "#{@INSTANCE_ID}", "#{pane_pid}"]),
        allow_failure=True,
    )
    out: list[tuple[str, int | None]] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pane_id, iid, pid_raw = parts
        if iid.strip() != instance_id:
            continue
        pid_raw = pid_raw.strip()
        pid = int(pid_raw) if pid_raw.isdigit() else None
        out.append((pane_id, pid))
    return out


def instance_live_tui(adapter: TmuxAdapter, instance_id: str, resolved_pane: str) -> LiveTui | None:
    """Live TUI for an instance: check the resolved pane, then a divergence sweep.

    Returns the :class:`LiveTui` of the live agent if one exists for this
    instance (so the guard refuses), or None when the instance is a genuinely
    reapable husk (no resolved-pane agent and no stamped live pane).
    """
    if resolved_pane:
        tui = detect_pane_tui(adapter, resolved_pane)
        if tui.live:
            return tui
    for pane_id, pid in _stamped_panes(adapter, instance_id):
        if resolved_pane and pane_id == resolved_pane:
            continue
        agent = active_agent_in_pane(pid)
        if agent is not None:
            agent_pid, command = agent
            return LiveTui(
                pane_id=pane_id,
                pane_pid=pid,
                agent_pid=agent_pid,
                agent_command=command,
            )
    return None
