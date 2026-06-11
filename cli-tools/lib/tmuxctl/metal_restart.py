"""DB-free metal restart executor (sandbox-scoped, parallel to executor.py).

v1 is **in-place resume**: for every live agent pane resolved by
``metal_resolver`` we terminate the agent process (the pane's shell survives —
agents are launched via send-keys into it), then relaunch onto the *same*
pane via the existing DB-free dispatch primitive::

    dispatch --id <resume_id> --engine <e> --dir <dir> --pane <%pane>

``--engine`` + ``--dir`` explicit makes dispatch skip DB metadata entirely;
we additionally point ``TOKEN_API_DB`` at a nonexistent path so even the
best-effort row lookup reads nothing. Shell panes are left untouched.

Hard guard: refuses ``--session main`` (and any session grouped with main).
This path is sandbox-only until explicitly graduated. Teardown+rebuild is the
M5 variant, added only once in-place resume is proven.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .metal_resolver import (
    MetalObservation,
    MetalProbe,
    find_agent_process,
    observe_and_resolve,
    read_process_table,
)

_AGENT_EXIT_TIMEOUT_SECONDS = 12.0
_AGENT_EXIT_POLL_SECONDS = 0.5
_POST_KILL_SETTLE_SECONDS = 1.0


class MetalRestartRefused(ValueError):
    """Raised when the target session is not a legal metal-restart target.

    Subclasses ValueError so cli.main()'s existing handler renders it as a
    clean one-line refusal (exit 1) instead of a traceback.
    """


@dataclass(frozen=True)
class MetalPaneResult:
    pane_id: str
    pane_label: str
    engine: str | None
    resume_id: str
    working_dir: str
    status: str  # resumed | would_resume | skipped | failed
    detail: str = ""


@dataclass(frozen=True)
class MetalRestartResult:
    session_name: str
    dry_run: bool
    results: tuple[MetalPaneResult, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(result.status != "failed" for result in self.results)


def _dispatch_bin() -> Path:
    return Path(__file__).resolve().parents[2] / "bin" / "dispatch"


def assert_legal_session(adapter, session_name: str) -> None:
    """Refuse main outright — the metal path is sandbox-only until graduated."""
    if session_name == "main":
        raise MetalRestartRefused(
            "metal-restart refuses --session main: sandbox-only until graduated"
        )
    sessions = adapter.list_sessions()
    groups_by_name = {row["session_name"]: row["session_group"] for row in sessions}
    group = groups_by_name.get(session_name, "")
    if group:
        siblings = {
            row["session_name"]
            for row in sessions
            if row["session_group"] == group and row["session_name"] != session_name
        }
        if "main" in siblings:
            raise MetalRestartRefused(
                f"metal-restart refuses {session_name}: session-grouped with main"
            )


def terminate_agent(
    agent_pid: int,
    *,
    table_reader: Callable[[], dict[int, tuple[int, str]]] = read_process_table,
    kill_fn: Callable[[int, int], None] = os.kill,
    sleep_fn: Callable[[float], None] = time.sleep,
    timeout_seconds: float = _AGENT_EXIT_TIMEOUT_SECONDS,
) -> bool:
    """SIGTERM the agent, escalate to SIGKILL, wait for it to leave the table."""
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            kill_fn(agent_pid, signum)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if agent_pid not in table_reader():
                return True
            sleep_fn(_AGENT_EXIT_POLL_SECONDS)
    return agent_pid not in table_reader()


def resume_pane(
    observation: MetalObservation,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> MetalPaneResult:
    """Relaunch a resolved agent onto its own pane via DB-free dispatch."""
    resume = observation.resume
    assert resume is not None
    env = dict(os.environ)
    # Belt and braces: dispatch already skips DB metadata when --engine/--dir
    # are explicit; a nonexistent DB path makes even the row lookup read nothing.
    env["TOKEN_API_DB"] = "/nonexistent/metal-restart-no-db.sqlite"
    proc = runner(
        [
            str(_dispatch_bin()),
            "--id",
            resume.resume_id,
            "--engine",
            resume.engine,
            "--dir",
            resume.working_dir,
            "--pane",
            resume.pane_id,
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return MetalPaneResult(
            pane_id=resume.pane_id,
            pane_label=resume.pane_label,
            engine=resume.engine,
            resume_id=resume.resume_id,
            working_dir=resume.working_dir,
            status="failed",
            detail=detail[-1] if detail else f"dispatch exit {proc.returncode}",
        )
    return MetalPaneResult(
        pane_id=resume.pane_id,
        pane_label=resume.pane_label,
        engine=resume.engine,
        resume_id=resume.resume_id,
        working_dir=resume.working_dir,
        status="resumed",
        detail=observation.reason,
    )


def metal_restart(
    adapter,
    session_name: str,
    *,
    dry_run: bool = False,
    probe: MetalProbe | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    table_reader: Callable[[], dict[int, tuple[int, str]]] = read_process_table,
    kill_fn: Callable[[int, int], None] = os.kill,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> MetalRestartResult:
    assert_legal_session(adapter, session_name)
    observations = observe_and_resolve(adapter, session_name, probe=probe)
    results: list[MetalPaneResult] = []
    for observation in observations:
        pane = observation.pane
        if observation.resume is None:
            # Bare shells stay untouched; unresolvable agents are NOT killed —
            # an open pane we cannot re-attach is worth more alive than blank.
            status = "skipped"
            results.append(
                MetalPaneResult(
                    pane_id=pane.pane_id,
                    pane_label=pane.pane_label,
                    engine=observation.engine,
                    resume_id="",
                    working_dir=pane.cwd,
                    status=status,
                    detail=observation.reason,
                )
            )
            continue
        if dry_run:
            resume = observation.resume
            results.append(
                MetalPaneResult(
                    pane_id=pane.pane_id,
                    pane_label=pane.pane_label,
                    engine=resume.engine,
                    resume_id=resume.resume_id,
                    working_dir=resume.working_dir,
                    status="would_resume",
                    detail=observation.reason,
                )
            )
            continue
        assert observation.agent_pid is not None
        if not terminate_agent(
            observation.agent_pid,
            table_reader=table_reader,
            kill_fn=kill_fn,
            sleep_fn=sleep_fn,
        ):
            results.append(
                MetalPaneResult(
                    pane_id=pane.pane_id,
                    pane_label=pane.pane_label,
                    engine=observation.engine,
                    resume_id=observation.resume.resume_id,
                    working_dir=observation.resume.working_dir,
                    status="failed",
                    detail=f"agent pid {observation.agent_pid} did not exit",
                )
            )
            continue
        # Let the wrapper shell unwind so dispatch sees a bare prompt.
        sleep_fn(_POST_KILL_SETTLE_SECONDS)
        # Sanity: confirm nothing agent-shaped is left under the pane.
        if find_agent_process(pane.pane_pid, table_reader()) is not None:
            results.append(
                MetalPaneResult(
                    pane_id=pane.pane_id,
                    pane_label=pane.pane_label,
                    engine=observation.engine,
                    resume_id=observation.resume.resume_id,
                    working_dir=observation.resume.working_dir,
                    status="failed",
                    detail="agent process still present under pane after kill",
                )
            )
            continue
        results.append(resume_pane(observation, runner=runner))
    return MetalRestartResult(session_name=session_name, dry_run=dry_run, results=tuple(results))


def render_metal_restart_result(result: MetalRestartResult) -> str:
    header = (
        f"metal-restart {result.session_name}"
        f"{' (dry-run)' if result.dry_run else ''}: "
        f"{sum(1 for r in result.results if r.status in ('resumed', 'would_resume'))} resumable, "
        f"{sum(1 for r in result.results if r.status == 'skipped')} skipped, "
        f"{sum(1 for r in result.results if r.status == 'failed')} failed"
    )
    lines = [header]
    for pane_result in result.results:
        label = pane_result.pane_label or pane_result.pane_id
        if pane_result.resume_id:
            lines.append(
                f"  {pane_result.status:>13} {label} {pane_result.engine} "
                f"{pane_result.resume_id} dir={pane_result.working_dir}"
                f"{' — ' + pane_result.detail if pane_result.detail else ''}"
            )
        else:
            lines.append(
                f"  {pane_result.status:>13} {label} "
                f"{pane_result.engine or 'shell'} — {pane_result.detail}"
            )
    return "\n".join(lines)
