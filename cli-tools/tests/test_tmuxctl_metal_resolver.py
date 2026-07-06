from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.metal_resolver import (
    MetalPane,
    MetalProbe,
    classify_engine,
    encode_claude_project_dir,
    find_agent_process,
    observe_session,
    resolve_claude_resume,
    resolve_codex_resume,
    resolve_resume,
)

CLAUDE_SESSION = "a4383c8b-5e2d-461a-8658-d96ff6db1141"
CLAUDE_SESSION_OLD = "0f0f0f0f-0000-0000-0000-000000000000"
CODEX_SESSION = "019eb293-c459-7b50-a19a-7fde0714f2a7"
CODEX_SESSION_OLD = "019deef6-e779-7812-ae81-288f6e8d4e24"


def _pane(
    pane_id: str = "%1", command: str = "bash", pane_pid: int = 100, cwd: str = "/scratch/a"
) -> MetalPane:
    return MetalPane(
        pane_id=pane_id,
        pane_label=f"metalrt:{pane_id.lstrip('%')}",
        window_index=1,
        window_name="metalrt",
        pane_index=1,
        current_command=command,
        pane_pid=pane_pid,
        cwd=cwd,
    )


def _write_claude_transcript(
    projects: pathlib.Path, cwd: str, session_id: str, mtime: float
) -> pathlib.Path:
    project_dir = projects / encode_claude_project_dir(cwd)
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    path.write_text(json.dumps({"type": "summary", "sessionId": session_id}) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def _write_codex_rollout(
    codex_home: pathlib.Path, session_id: str, cwd: str, mtime: float, day: str = "2026/06/10"
) -> pathlib.Path:
    day_dir = codex_home / "sessions" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-06-10T10-28-09-{session_id}.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": cwd}}) + "\n"
    )
    os.utime(path, (mtime, mtime))
    return path


def _write_codex_index(codex_home: pathlib.Path, rows: list[dict]) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    index = codex_home / "session_index.jsonl"
    index.write_text("".join(json.dumps(row) + "\n" for row in rows))


# --- engine classification ---------------------------------------------------


def test_classify_engine_exact_basenames():
    assert (
        classify_engine("/Users/x/.local/bin/claude --dangerously-skip-permissions -r") == "claude"
    )
    assert classify_engine("/usr/local/bin/codex resume abc") == "codex"
    assert classify_engine("bash /x/cli-tools/scripts/agent-wrapper.sh claude --foo") is None
    assert classify_engine("zsh") is None
    assert classify_engine("") is None


def test_find_agent_process_walks_through_wrapper():
    # pane shell -> wrapper bash -> claude binary (the live shape on mac).
    table = {
        100: (1, "zsh"),
        101: (
            100,
            "bash /live/cli-tools/scripts/agent-wrapper.sh claude --dangerously-skip-permissions",
        ),
        102: (101, "/Users/x/.local/bin/claude --dangerously-skip-permissions"),
        103: (102, "npm exec chrome-devtools-mcp"),
    }
    assert find_agent_process(100, table) == ("claude", 102)


def test_find_agent_process_codex_direct_child():
    table = {
        200: (1, "zsh"),
        201: (200, "/usr/local/bin/codex resume -C /scratch/b 019e-abc"),
    }
    assert find_agent_process(200, table) == ("codex", 201)


def test_find_agent_process_none_for_bare_shell():
    table = {300: (1, "zsh"), 301: (300, "vim notes.md")}
    assert find_agent_process(300, table) is None


# --- claude resolution --------------------------------------------------------


def test_resolve_claude_resume_newest_transcript_wins(tmp_path):
    projects = tmp_path / "projects"
    _write_claude_transcript(projects, "/scratch/a", CLAUDE_SESSION_OLD, mtime=1_000)
    _write_claude_transcript(projects, "/scratch/a", CLAUDE_SESSION, mtime=2_000)
    resume_id, reason = resolve_claude_resume("/scratch/a", projects)
    assert resume_id == CLAUDE_SESSION
    assert CLAUDE_SESSION in reason


def test_resolve_claude_resume_basename_fallback_on_bad_json(tmp_path):
    projects = tmp_path / "projects"
    project_dir = projects / encode_claude_project_dir("/scratch/a")
    project_dir.mkdir(parents=True)
    (project_dir / f"{CLAUDE_SESSION}.jsonl").write_text("not-json\n")
    resume_id, _reason = resolve_claude_resume("/scratch/a", projects)
    assert resume_id == CLAUDE_SESSION


def test_resolve_claude_resume_missing_project_dir(tmp_path):
    resume_id, reason = resolve_claude_resume("/scratch/nowhere", tmp_path / "projects")
    assert resume_id is None
    assert "no claude project dir" in reason


def test_encode_claude_project_dir_matches_transplant_tr():
    # transplant's encode_claude_path: tr '/.' '-' — leading dash retained.
    assert (
        encode_claude_project_dir("/Volumes/Imperium/Imperium-ENV")
        == "-Volumes-Imperium-Imperium-ENV"
    )
    assert encode_claude_project_dir("/Users/x/.openclaw") == "-Users-x--openclaw"


# --- codex resolution ----------------------------------------------------------


def test_resolve_codex_resume_index_newest_cwd_match(tmp_path):
    codex_home = tmp_path / "codex"
    _write_codex_rollout(codex_home, CODEX_SESSION_OLD, "/scratch/b", mtime=1_000)
    _write_codex_rollout(codex_home, CODEX_SESSION, "/scratch/b", mtime=2_000)
    _write_codex_index(
        codex_home,
        [
            {"id": CODEX_SESSION_OLD, "thread_name": "old", "updated_at": "2026-06-01T00:00:00Z"},
            {"id": CODEX_SESSION, "thread_name": "new", "updated_at": "2026-06-10T17:28:11Z"},
        ],
    )
    resume_id, reason = resolve_codex_resume("/scratch/b", codex_home)
    assert resume_id == CODEX_SESSION
    assert "session_index" in reason


def test_resolve_codex_resume_index_skips_other_cwd(tmp_path):
    codex_home = tmp_path / "codex"
    _write_codex_rollout(codex_home, CODEX_SESSION, "/scratch/other", mtime=2_000)
    _write_codex_rollout(codex_home, CODEX_SESSION_OLD, "/scratch/b", mtime=1_000)
    _write_codex_index(
        codex_home,
        [
            {"id": CODEX_SESSION, "thread_name": "new", "updated_at": "2026-06-10T17:28:11Z"},
            {"id": CODEX_SESSION_OLD, "thread_name": "old", "updated_at": "2026-06-01T00:00:00Z"},
        ],
    )
    resume_id, _reason = resolve_codex_resume("/scratch/b", codex_home)
    assert resume_id == CODEX_SESSION_OLD


def test_resolve_codex_resume_mtime_fallback_without_index(tmp_path):
    codex_home = tmp_path / "codex"
    _write_codex_rollout(codex_home, CODEX_SESSION_OLD, "/scratch/b", mtime=1_000)
    _write_codex_rollout(codex_home, CODEX_SESSION, "/scratch/b", mtime=2_000)
    resume_id, reason = resolve_codex_resume("/scratch/b", codex_home)
    assert resume_id == CODEX_SESSION
    assert "mtime scan" in reason


def test_resolve_codex_resume_no_match(tmp_path):
    codex_home = tmp_path / "codex"
    _write_codex_rollout(codex_home, CODEX_SESSION, "/scratch/other", mtime=2_000)
    resume_id, reason = resolve_codex_resume("/scratch/b", codex_home)
    assert resume_id is None
    assert "no codex rollout" in reason


# --- end-to-end pane resolution -------------------------------------------------


def _probe(tmp_path, table, agent_cwds=None) -> MetalProbe:
    agent_cwds = agent_cwds or {}
    return MetalProbe(
        process_table=table,
        process_cwd=lambda pid: agent_cwds.get(pid),
        claude_projects=tmp_path / "projects",
        codex_home=tmp_path / "codex",
    )


def test_resolve_resume_claude_pane(tmp_path):
    _write_claude_transcript(tmp_path / "projects", "/scratch/a", CLAUDE_SESSION, mtime=2_000)
    table = {
        100: (1, "zsh"),
        101: (100, "bash /live/cli-tools/scripts/agent-wrapper.sh claude"),
        102: (101, "/Users/x/.local/bin/claude --resume"),
    }
    observation = resolve_resume(_pane(pane_pid=100, cwd="/scratch/a"), _probe(tmp_path, table))
    assert observation.engine == "claude"
    assert observation.resume is not None
    assert observation.resume.resume_id == CLAUDE_SESSION
    assert observation.resume.working_dir == "/scratch/a"


def test_resolve_resume_codex_pane_prefers_process_cwd(tmp_path):
    # codex -C /scratch/b: the engine's cwd diverges from the pane shell's cwd.
    codex_home = tmp_path / "codex"
    _write_codex_rollout(codex_home, CODEX_SESSION, "/scratch/b", mtime=2_000)
    _write_codex_index(
        codex_home,
        [{"id": CODEX_SESSION, "thread_name": "t", "updated_at": "2026-06-10T17:28:11Z"}],
    )
    table = {200: (1, "zsh"), 201: (200, "/usr/local/bin/codex")}
    probe = _probe(tmp_path, table, agent_cwds={201: "/scratch/b"})
    observation = resolve_resume(_pane(pane_pid=200, cwd="/Users/x"), probe)
    assert observation.engine == "codex"
    assert observation.resume is not None
    assert observation.resume.resume_id == CODEX_SESSION
    assert observation.resume.working_dir == "/scratch/b"


def test_resolve_resume_shell_pane_not_resumable(tmp_path):
    table = {300: (1, "zsh")}
    observation = resolve_resume(_pane(pane_pid=300, command="zsh"), _probe(tmp_path, table))
    assert observation.engine is None
    assert observation.resume is None
    assert "no agent process" in observation.reason


def test_resolve_resume_agent_without_transcript(tmp_path):
    table = {400: (1, "zsh"), 401: (400, "/Users/x/.local/bin/claude")}
    observation = resolve_resume(_pane(pane_pid=400, cwd="/scratch/empty"), _probe(tmp_path, table))
    assert observation.engine == "claude"
    assert observation.resume is None


# --- tmux observation (fake adapter) ---------------------------------------------


class _FakeAdapter:
    def __init__(self, lines: list[str], options: dict[tuple[str, str], str]):
        self._lines = lines
        self._options = options
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, **_kwargs) -> str:
        self.calls.append(args)
        assert args[0] == "list-panes", "observe_session must stay read-only"
        return "\n".join(self._lines) + "\n"

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self._options.get((pane_id, option), "")


def test_observe_session_parses_panes():
    adapter = _FakeAdapter(
        lines=[
            "%1\t1\tmetalrt\t1\tbash\t100\t/scratch/a",
            "%2\t1\tmetalrt\t2\tcodex\t200\t/scratch/b",
        ],
        options={("%1", "@PANE_ID"): "metalrt:A", ("%2", "@PANE_ID"): "metalrt:B"},
    )
    panes = observe_session(adapter, "metalrt")
    assert [pane.pane_label for pane in panes] == ["metalrt:A", "metalrt:B"]
    assert panes[0].pane_pid == 100
    assert panes[1].cwd == "/scratch/b"
    assert panes[1].current_command == "codex"


def test_observe_session_raises_on_malformed_tmux_rows() -> None:
    adapter = _FakeAdapter(lines=["%1\t1\tmetalrt"], options={})

    with pytest.raises(ValueError):
        observe_session(adapter, "metalrt")


def test_observe_session_raises_on_non_integer_pane_pid() -> None:
    adapter = _FakeAdapter(
        lines=["%1\t1\tmetalrt\t1\tbash\tnot-a-pid\t/scratch/a"],
        options={("%1", "@PANE_ID"): "metalrt:A"},
    )

    with pytest.raises(ValueError):
        observe_session(adapter, "metalrt")
