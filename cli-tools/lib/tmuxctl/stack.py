"""Stack window management — auto-scaling pane stacks for legion/mechanicus.

Stack windows hold a vertical list of worker panes. Pane 1 of each stack window
is the orchestrator anchor (custodes for legion, fabricator-general for
mechanicus); panes 2..N are workers added by dispatch.

Auto-scaling: when tmux refuses split-window in the canonical window because
the geometry can't fit another pane, we spill into a sibling window suffixed
`-N` (legion -> legion-2 -> legion-3, ...). The visible set of stack panes
spans all of these windows, so dispatch is never blocked by a single window's
geometric ceiling.

send-keys safety: this module never resizes existing panes. A new pane is just
a fresh split that takes its share of the host window's height; existing panes
shrink only as much as tmux's even-vertical default redistributes. The global
`pane-border-status top` setting (cli-tools/tmux/tmux-base.conf) renders each
pane's title above its content so the stack reads as a labeled list. Full
visibility for any pane is reached via the existing audience-chamber system
(see audience.py).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable

from .api import fetch_instance_rows_raw, rebind_instance_pane
from .tmux_adapter import TmuxAdapter, TmuxError

# Instance statuses that denote a live, drive-able runtime worth rebinding.
_LIVE_STATUSES = frozenset({"processing", "idle"})

STACK_BASES: tuple[str, ...] = ("legion", "mechanicus", "mars", "kreig")
SPILL_RE = re.compile(r"^(?P<base>[a-z]+)(?:-(?P<n>\d+))?$")


def is_stack_base(window_name: str) -> bool:
    """True if a window name (canonical or spill suffix) is a stack window."""
    base = stack_base_of(window_name)
    return base in STACK_BASES


def stack_base_of(window_name: str) -> str:
    """Return the canonical base for a window name; '' if not a stack pattern."""
    m = SPILL_RE.match(window_name)
    if not m:
        return ""
    return m.group("base")


def _spill_index(window_name: str) -> int:
    m = SPILL_RE.match(window_name)
    if not m:
        return 0
    n = m.group("n")
    return int(n) if n else 1


def _spill_name(base: str, n: int) -> str:
    return base if n == 1 else f"{base}-{n}"


def _list_spill_windows(adapter: TmuxAdapter, session: str, base: str) -> list[str]:
    """Return existing windows for `base`, sorted by spill index ascending."""
    rows = adapter.run(
        "list-windows", "-t", session, "-F", "#{window_name}", allow_failure=True
    ).splitlines()
    spills: list[str] = []
    for raw in rows:
        # tmux may suffix names with marker chars; strip parenthesized markers
        name = raw.split("(", 1)[0]
        if stack_base_of(name) == base:
            spills.append(name)
    spills.sort(key=_spill_index)
    return spills


def _try_split(adapter: TmuxAdapter, target: str, cwd: str) -> str | None:
    """Attempt to split a new pane; return pane_id or None on geometric failure."""
    try:
        out = adapter.run(
            "split-window", "-t", target, "-d", "-P", "-F", "#{pane_id}", "-c", cwd
        ).strip()
    except TmuxError:
        return None
    return out or None


def _create_spill_window(adapter: TmuxAdapter, session: str, name: str, cwd: str) -> str:
    """Create a new spillover window and return its first pane_id."""
    adapter.run("new-window", "-t", session, "-n", name, "-d", "-c", cwd)
    return adapter.run("display-message", "-t", f"{session}:{name}", "-p", "#{pane_id}").strip()


def _tag_worker(adapter: TmuxAdapter, pane_id: str, base: str) -> None:
    """Tag a freshly added stack worker pane for downstream tools."""
    adapter.run("set-option", "-p", "-t", pane_id, "@PANE_TYPE", "stack-worker", allow_failure=True)
    adapter.run("set-option", "-p", "-t", pane_id, "@PANE_ID", f"{base}:worker", allow_failure=True)


def add_stack_pane(
    adapter: TmuxAdapter,
    session: str,
    base: str,
    *,
    cwd: str | None = None,
) -> str:
    """Add a new worker pane to the named stack, spilling if the canonical window is full.

    Returns the new pane id. Raises ValueError if `base` is not a known stack window.
    """
    if base not in STACK_BASES:
        raise ValueError(f"not a stack window: {base}")
    cwd = cwd or os.path.expanduser("~")

    if base in {"legion", "mechanicus"}:
        from .stack import add_orchestrator_stack_pane

        return add_orchestrator_stack_pane(adapter, session, base, cwd=cwd)

    existing = _list_spill_windows(adapter, session, base)
    if not existing:
        # No canonical window yet — create it as the first stack window.
        pane = _create_spill_window(adapter, session, base, cwd)
        _tag_worker(adapter, pane, base)
        return pane

    # Try each existing spill window in order; use the first that accepts a split.
    for win in existing:
        target = f"{session}:{win}"
        pane = _try_split(adapter, target, cwd)
        if pane:
            _tag_worker(adapter, pane, base)
            return pane

    # All existing spill windows are geometrically full. Create the next one.
    next_n = _spill_index(existing[-1]) + 1
    new_name = _spill_name(base, next_n)
    pane = _create_spill_window(adapter, session, new_name, cwd)
    _tag_worker(adapter, pane, base)
    return pane


def dispatch_stack_command(
    adapter: TmuxAdapter,
    session: str,
    base: str,
    command: str,
    *,
    cwd: str | None = None,
    focus: bool = True,
    settle_seconds: float = 0.5,
) -> str:
    """Create one managed stack worker pane and run a command in it.

    This is the pane-backed dispatch primitive for legion/mechanicus worker
    launches. Callers may still use ``stack add`` when they need to stage their
    own input, but entry points that create-and-launch work should route through
    this function instead of doing raw ``tmux split-window`` themselves.
    """
    from .focus_guard import preserve_focus

    with preserve_focus(
        adapter,
        source="tmuxctl stack dispatch",
        attempted_target=f"{session}:{base}",
        enabled=os.environ.get("IMPERIUM_ALLOW_TMUX_FOCUS") != "1",
    ):
        pane = add_stack_pane(adapter, session, base, cwd=cwd)
        if focus:
            window_target = adapter.run(
                "display-message",
                "-t",
                pane,
                "-p",
                "#{session_name}:#{window_name}",
                allow_failure=True,
            ).strip()
            if window_target:
                adapter.run("select-window", "-t", window_target, allow_failure=True)
            adapter.run("select-pane", "-t", pane, allow_failure=True)
            if base in {"legion", "mechanicus"}:
                target = adapter.run(
                    "display-message",
                    "-t",
                    pane,
                    "-p",
                    "#{session_name}:#{window_index}",
                    allow_failure=True,
                ).strip()
                if target:
                    enforce_stack_layout(adapter, target, focused_pane=pane, focus=True)
        if settle_seconds > 0:
            time.sleep(settle_seconds)
        # Gated send: send_keys routes through TmuxAdapter.run()'s universal gate.
        adapter.send_keys(pane, command, "Enter")
        return pane


def _is_token_pane(value: str) -> bool:
    """True if a ``tmux_pane`` value is an allocation token (``mechanicus:new``)
    rather than a concrete tmux pane id (``%NN``).

    Concrete pane ids always start with ``%``; the dispatch launch path used to
    bake the allocation request token as the pane, leaving the registry row
    unresolvable by pane_truth / assert-instance / agent-cmd.
    """
    return bool(value) and not value.startswith("%")


def _descendant_pids(root_pid: int, children_by_ppid: dict[int, list[int]]) -> set[int]:
    """All pids in `root_pid`'s process subtree, including `root_pid` itself."""
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children_by_ppid.get(pid, []))
    return seen


def reconcile_token_valued_panes(
    worker_pane_pids: dict[str, int],
    rows: Iterable[dict],
    children_by_ppid: dict[int, list[int]],
    *,
    rebind,
) -> list[tuple[str, str]]:
    """Rebind drifted instance rows to the concrete pane their process runs in.

    A row is rebindable when its ``tmux_pane`` is an allocation token, its status
    is live, and its recorded ``pid`` lives in the process subtree of exactly one
    live stack-worker pane. PID is the only correlation key reliably present on
    both sides (the row carries the wrapper pid; the pane carries its pane_pid),
    so it handles both rows that drifted before the launch-path fix and any future
    regression — no extra pane tagging required.

    Returns the ``(instance_id, pane_id)`` rebinds performed.
    """
    pane_pid_sets = {
        pane_id: _descendant_pids(pane_pid, children_by_ppid)
        for pane_id, pane_pid in worker_pane_pids.items()
    }

    rebinds: list[tuple[str, str]] = []
    for row in rows:
        if not _is_token_pane(str(row.get("tmux_pane") or "")):
            continue
        if str(row.get("status") or "") not in _LIVE_STATUSES:
            continue
        try:
            pid = int(row.get("pid"))
        except (TypeError, ValueError):
            continue
        instance_id = str(row.get("id") or "")
        if not instance_id:
            continue
        for pane_id, pid_set in pane_pid_sets.items():
            if pid in pid_set:
                rebind(instance_id, pane_id)
                rebinds.append((instance_id, pane_id))
                break
    return rebinds


def _stack_worker_pane_pids(adapter: TmuxAdapter, session: str) -> dict[str, int]:
    """Map live stack-worker pane ids -> pane_pid across all stack windows."""
    from ._stack_core import _stack_panes
    from .custodes import _pane_pid

    out: dict[str, int] = {}
    rows = adapter.run(
        "list-windows",
        "-t",
        session,
        "-F",
        "#{window_index}\t#{window_name}",
        allow_failure=True,
    ).splitlines()
    for row in rows:
        if "\t" not in row:
            continue
        index, raw_name = row.split("\t", 1)
        name = raw_name.split("(", 1)[0]
        if not is_stack_base(name):
            continue
        for pane in _stack_panes(adapter, f"{session}:{index}"):
            if pane.pane_type != "stack-worker":
                continue
            pid = _pane_pid(adapter, pane.pane_id)
            if pid:
                out[pane.pane_id] = pid
    return out


def reconcile_stack_pane_registry(
    adapter: TmuxAdapter, session: str = "main"
) -> list[tuple[str, str]]:
    """Self-heal instance rows whose ``tmux_pane`` is an allocation token.

    Best-effort backstop for the dispatch pane-registry wedge: rebinds rows that
    drifted before the launch-env fix shipped (and any future regression) to the
    concrete pane their process runs in. Gathers live stack-worker panes first so
    a session with no workers never touches the registry; never raises — registry
    or tmux IO failures degrade to a no-op.
    """
    try:
        worker_pane_pids = _stack_worker_pane_pids(adapter, session)
        if not worker_pane_pids:
            return []
        drifted = [
            row
            for row in fetch_instance_rows_raw()
            if _is_token_pane(str(row.get("tmux_pane") or ""))
            and str(row.get("status") or "") in _LIVE_STATUSES
        ]
        if not drifted:
            return []
        from .custodes import _process_tree

        children_by_ppid, _ = _process_tree()
        return reconcile_token_valued_panes(
            worker_pane_pids, drifted, children_by_ppid, rebind=rebind_instance_pane
        )
    except Exception:
        return []


def sweep_stack_assertions(
    adapter: TmuxAdapter,
    session: str = "main",
    *,
    kill_pending_clear: bool = True,
) -> str:
    """Assert/prune every active managed stack window in a session.

    Invocation-scoped assertion only repairs panes that a caller targets. This
    sweep walks all live stack pages (including spill windows) and delegates to
    enforce_stack_layout, which retags clear unknown worker artifacts and prunes
    dead stack-worker panes. Use from cron/hook surfaces as the low-friction
    periodic cleanup pass.
    """
    from .focus_guard import preserve_focus

    with preserve_focus(
        adapter,
        source="tmuxctl stack sweep",
        attempted_target=session,
        enabled=os.environ.get("IMPERIUM_ALLOW_TMUX_FOCUS") != "1",
    ):
        rows = adapter.run(
            "list-windows",
            "-t",
            session,
            "-F",
            "#{window_index}\t#{window_name}",
            allow_failure=True,
        ).splitlines()
        results: list[str] = []
        for row in rows:
            if "\t" not in row:
                continue
            index, raw_name = row.split("\t", 1)
            name = raw_name.split("(", 1)[0]
            if not is_stack_base(name):
                continue
            target = f"{session}:{index}"
            try:
                results.append(
                    enforce_stack_layout(
                        adapter,
                        target,
                        kill_pending_clear=kill_pending_clear,
                    )
                )
            except Exception as exc:
                results.append(f"sweep failed {target}: {exc}")
        for instance_id, pane_id in reconcile_stack_pane_registry(adapter, session):
            results.append(f"rebound {instance_id[:12]} -> {pane_id}")
        return "\n".join(results) if results else f"no stack windows in {session}"


# Generic persona-pane stack page implementation. Imported at module end so the
# implementation can reuse stack_base_of()/spill helpers above without a cycle.
from ._stack_core import (  # noqa: E402,F401
    CUSTODES_ROLE,
    FABRICATOR_ROLE,
    LEGACY_WORKER_ROLES,
    REGIMENT_ROLE,
    STACK_COLLAPSED_HEIGHT,
    STACK_ORCHESTRATOR_RATIO,
    STACK_PAGE_SPECS,
    PersonaPaneSpec,
    StackPageSpec,
    StackPane,
    add_orchestrator_stack_pane,
    add_stack_worker_pane,
    enforce_stack_layout,
    enforce_stack_page_layout,
    focus_selected,
)
