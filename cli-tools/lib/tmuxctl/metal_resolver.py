"""Metal-first restart observation: resume identity from live tmux + filesystem.

The DB-anchored restart path (planner.py / executor.py) resumes from
``/api/instances`` and is only as good as the registry — which is churned out
from under us (``_unstamp_instance_id``, post-Slice-B ``pane_label`` loss,
stale status) and structurally blind to codex (own store under ``~/.codex``).

This module is the parallel, DB-free observation layer: the literal live panes
are the source of truth. Per pane we read only tmux (``pane_pid``,
``pane_current_path``, ``@PANE_ID``), the process table (``ps``), and the
engines' own transcript stores:

- **claude** — cwd -> ``~/.claude/projects/<encoded-cwd>/`` -> newest
  ``*.jsonl`` by mtime -> ``sessionId`` (first line; basename == sessionId).
  We resume whatever transcript the pane is *currently writing*, so the
  ``@INSTANCE_ID`` stamp churn is irrelevant here.
- **codex** — ``~/.codex/session_index.jsonl`` rows ``{id, thread_name,
  updated_at}`` newest-first, correlated to the pane's cwd via the rollout
  file's first-line ``payload.cwd`` (``~/.codex/sessions/<Y>/<M>/<D>/
  rollout-*-<uuid>.jsonl``). Falls back to a newest-mtime rollout scan when
  the index is absent or has no cwd match — verified live 2026-06-11: codex
  0.139 leaves ``session_index.jsonl`` stale (last row 2026-05-03), so the
  mtime scan is the path that actually carries current sessions.

Resume semantics (verified live 2026-06-11 in the metalrt sandbox): both
``claude --resume <id>`` and ``codex resume <id>`` re-attach to and APPEND to
the original transcript file — same sessionId/rollout, no fork.

Known limitation (accepted): two live agents of the same engine sharing one
cwd resolve to the single newest transcript for that cwd. Sandbox panes use
distinct scratch dirs; graduation beyond that needs a per-process tiebreak.

Zero ``/api/instances`` calls, zero ``agents.db`` reads. Read-only.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .tmux_adapter import TmuxAdapter

ENGINE_CLAUDE = "claude"
ENGINE_CODEX = "codex"

# Filename shape: rollout-<timestamp>-<uuid36>.jsonl
_ROLLOUT_PREFIX = "rollout-"
_UUID_LEN = 36


@dataclass(frozen=True)
class MetalPane:
    """One live pane as observed from tmux alone."""

    pane_id: str
    pane_label: str
    window_index: int
    window_name: str
    pane_index: int
    current_command: str
    pane_pid: int
    cwd: str


@dataclass(frozen=True)
class MetalResume:
    """A resumable agent resolved purely from the metal."""

    engine: str
    resume_id: str
    working_dir: str
    pane_id: str
    pane_label: str
    disposition: str = "resume"


@dataclass(frozen=True)
class MetalObservation:
    """Pane + engine classification + resume resolution (or the reason why not)."""

    pane: MetalPane
    engine: str | None
    agent_pid: int | None
    resume: MetalResume | None
    reason: str


@dataclass(frozen=True)
class MetalProbe:
    """Injected filesystem/process readers so resolution unit-tests offline.

    ``process_table`` maps pid -> (ppid, command line); ``process_cwd``
    returns a pid's working directory or None. Roots point at the engines'
    transcript stores.
    """

    process_table: dict[int, tuple[int, str]]
    process_cwd: Callable[[int], str | None]
    claude_projects: Path
    codex_home: Path

    @classmethod
    def live(cls) -> MetalProbe:
        home = Path.home()
        return cls(
            process_table=read_process_table(),
            process_cwd=read_process_cwd,
            claude_projects=home / ".claude" / "projects",
            codex_home=home / ".codex",
        )


def read_process_table() -> dict[int, tuple[int, str]]:
    """Snapshot the live process table via ps (macOS has no /proc)."""
    out = subprocess.run(
        ["ps", "-ax", "-o", "pid=,ppid=,command="],
        text=True,
        capture_output=True,
        check=False,
    ).stdout
    table: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, parts[2].strip())
    return table


def read_process_cwd(pid: int) -> str | None:
    """A process's actual cwd via lsof (codex -C may differ from the pane path)."""
    out = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        text=True,
        capture_output=True,
        check=False,
    ).stdout
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def classify_engine(command: str) -> str | None:
    """claude/codex iff the argv0 basename matches exactly (wrappers excluded)."""
    if not command:
        return None
    argv0 = command.split()[0]
    basename = Path(argv0).name
    if basename in (ENGINE_CLAUDE, ENGINE_CODEX):
        return basename
    return None


def find_agent_process(
    pane_pid: int, process_table: dict[int, tuple[int, str]]
) -> tuple[str, int] | None:
    """Breadth-first walk of the pane's descendants for the first claude/codex.

    Wrapper-launched agents (claude-wrapper.sh, dispatch inline codex) put a
    shell between pane_pid and the engine binary, so ``pane_current_command``
    reads ``bash`` — the engine truth lives deeper in the tree.
    """
    children: dict[int, list[int]] = {}
    for pid, (ppid, _command) in process_table.items():
        children.setdefault(ppid, []).append(pid)
    frontier = sorted(children.get(pane_pid, []))
    seen: set[int] = set()
    while frontier:
        next_frontier: list[int] = []
        for pid in frontier:
            if pid in seen:
                continue
            seen.add(pid)
            engine = classify_engine(process_table[pid][1])
            if engine is not None:
                return engine, pid
            next_frontier.extend(sorted(children.get(pid, [])))
        frontier = next_frontier
    return None


def encode_claude_project_dir(working_dir: str) -> str:
    """Mirror transplant's encode_claude_path: ``tr '/.' '-'``."""
    return working_dir.replace("/", "-").replace(".", "-")


def resolve_claude_resume(working_dir: str, claude_projects: Path) -> tuple[str | None, str]:
    """Newest transcript for the cwd's project slug -> sessionId."""
    project_dir = claude_projects / encode_claude_project_dir(working_dir)
    if not project_dir.is_dir():
        return None, f"no claude project dir for cwd: {project_dir.name}"
    transcripts = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not transcripts:
        return None, f"no transcripts in {project_dir.name}"
    newest = transcripts[0]
    session_id = _claude_transcript_session_id(newest)
    if session_id:
        return session_id, f"newest transcript {newest.name}"
    return newest.stem, f"newest transcript {newest.name} (basename fallback)"


def _claude_transcript_session_id(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as handle:
            first = handle.readline()
        record = json.loads(first)
    except (OSError, json.JSONDecodeError):
        return None
    session_id = record.get("sessionId")
    return session_id if isinstance(session_id, str) and session_id else None


def _rollout_uuid(path: Path) -> str | None:
    stem = path.stem
    if not stem.startswith(_ROLLOUT_PREFIX) or len(stem) < _UUID_LEN:
        return None
    return stem[-_UUID_LEN:]


def _rollout_cwd(path: Path) -> str | None:
    """First-line ``payload.cwd`` of a codex rollout file (session_meta)."""
    try:
        with path.open(encoding="utf-8") as handle:
            first = handle.readline()
        record = json.loads(first)
    except (OSError, json.JSONDecodeError):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _codex_rollout_map(sessions_root: Path) -> dict[str, Path]:
    rollouts: dict[str, Path] = {}
    if not sessions_root.is_dir():
        return rollouts
    for path in sessions_root.rglob("rollout-*.jsonl"):
        uuid = _rollout_uuid(path)
        if uuid:
            rollouts[uuid] = path
    return rollouts


def resolve_codex_resume(working_dir: str, codex_home: Path) -> tuple[str | None, str]:
    """Newest session_index row whose rollout payload.cwd matches the pane cwd."""
    target = os.path.normpath(working_dir)
    rollouts = _codex_rollout_map(codex_home / "sessions")
    index_path = codex_home / "session_index.jsonl"
    if index_path.is_file():
        rows: list[dict] = []
        try:
            with index_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("id"), str):
                        rows.append(row)
        except OSError:
            rows = []
        rows.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
        for row in rows:
            rollout = rollouts.get(row["id"])
            if rollout is None:
                continue
            cwd = _rollout_cwd(rollout)
            if cwd is not None and os.path.normpath(cwd) == target:
                return row["id"], f"session_index updated_at={row.get('updated_at', '?')}"
    # Index absent (or no cwd match through it): newest rollout by mtime.
    by_mtime = sorted(
        rollouts.items(),
        key=lambda item: item[1].stat().st_mtime,
        reverse=True,
    )
    for uuid, rollout in by_mtime:
        cwd = _rollout_cwd(rollout)
        if cwd is not None and os.path.normpath(cwd) == target:
            return uuid, f"rollout mtime scan {rollout.name}"
    return None, f"no codex rollout with cwd {target}"


def resolve_resume(pane: MetalPane, probe: MetalProbe) -> MetalObservation:
    """Classify one pane and resolve its resume identity from the metal."""
    agent = find_agent_process(pane.pane_pid, probe.process_table)
    if agent is None:
        return MetalObservation(
            pane=pane,
            engine=None,
            agent_pid=None,
            resume=None,
            reason=f"no agent process under pane_pid {pane.pane_pid} ({pane.current_command})",
        )
    engine, agent_pid = agent
    # The engine process's own cwd beats the pane path (codex -C divergence).
    working_dir = probe.process_cwd(agent_pid) or pane.cwd
    if engine == ENGINE_CLAUDE:
        resume_id, reason = resolve_claude_resume(working_dir, probe.claude_projects)
    else:
        resume_id, reason = resolve_codex_resume(working_dir, probe.codex_home)
    if resume_id is None:
        return MetalObservation(
            pane=pane, engine=engine, agent_pid=agent_pid, resume=None, reason=reason
        )
    return MetalObservation(
        pane=pane,
        engine=engine,
        agent_pid=agent_pid,
        resume=MetalResume(
            engine=engine,
            resume_id=resume_id,
            working_dir=working_dir,
            pane_id=pane.pane_id,
            pane_label=pane.pane_label,
        ),
        reason=reason,
    )


def observe_session(adapter: TmuxAdapter, session_name: str) -> list[MetalPane]:
    """Walk a session's live panes via tmux only (read-only)."""
    fmt = "\t".join(
        [
            "#{pane_id}",
            "#{window_index}",
            "#{window_name}",
            "#{pane_index}",
            "#{pane_current_command}",
            "#{pane_pid}",
            "#{pane_current_path}",
        ]
    )
    lines = adapter.run("list-panes", "-s", "-t", session_name, "-F", fmt).splitlines()
    panes: list[MetalPane] = []
    for line in lines:
        pane_id, window_index, window_name, pane_index, command, pane_pid, cwd = line.split("\t")
        panes.append(
            MetalPane(
                pane_id=pane_id,
                pane_label=adapter.show_pane_option(pane_id, "@PANE_ID"),
                window_index=int(window_index),
                window_name=window_name,
                pane_index=int(pane_index),
                current_command=command,
                pane_pid=int(pane_pid),
                cwd=cwd,
            )
        )
    return panes


def observe_and_resolve(
    adapter: TmuxAdapter, session_name: str, probe: MetalProbe | None = None
) -> list[MetalObservation]:
    probe = probe or MetalProbe.live()
    return [resolve_resume(pane, probe) for pane in observe_session(adapter, session_name)]


def observation_to_dict(observation: MetalObservation) -> dict:
    pane = observation.pane
    payload: dict = {
        "pane_id": pane.pane_id,
        "pane_label": pane.pane_label,
        "window": f"{pane.window_name}:{pane.pane_index}",
        "current_command": pane.current_command,
        "cwd": pane.cwd,
        "engine": observation.engine,
        "agent_pid": observation.agent_pid,
        "reason": observation.reason,
        "resume": None,
    }
    if observation.resume is not None:
        payload["resume"] = {
            "engine": observation.resume.engine,
            "resume_id": observation.resume.resume_id,
            "working_dir": observation.resume.working_dir,
            "disposition": observation.resume.disposition,
        }
    return payload


def render_observations(observations: list[MetalObservation]) -> str:
    lines = []
    for observation in observations:
        pane = observation.pane
        label = pane.pane_label or pane.pane_id
        if observation.resume is not None:
            lines.append(
                f"{label} [{pane.window_name}.{pane.pane_index}] "
                f"{observation.engine} resume={observation.resume.resume_id} "
                f"dir={observation.resume.working_dir} ({observation.reason})"
            )
        else:
            engine = observation.engine or "shell"
            lines.append(
                f"{label} [{pane.window_name}.{pane.pane_index}] "
                f"{engine} not-resumable ({observation.reason})"
            )
    return "\n".join(lines)
