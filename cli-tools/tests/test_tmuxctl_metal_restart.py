from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.metal_resolver import MetalProbe, encode_claude_project_dir
from tmuxctl.metal_restart import (
    MetalRestartRefused,
    assert_legal_session,
    metal_restart,
    render_metal_restart_result,
    terminate_agent,
)

CLAUDE_SESSION = "a4383c8b-5e2d-461a-8658-d96ff6db1141"
CODEX_SESSION = "019eb293-c459-7b50-a19a-7fde0714f2a7"


class _FakeAdapter:
    """list-panes + pane options + sessions; records every tmux invocation."""

    def __init__(self, pane_lines: list[str], options=None, sessions=None):
        self._pane_lines = pane_lines
        self._options = options or {}
        self._sessions = sessions or [
            {
                "session_name": "metalrt",
                "session_group": "",
                "window_index": "1",
                "window_name": "metalrt",
            }
        ]
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, **_kwargs) -> str:
        self.calls.append(args)
        if args[0] == "list-panes":
            return "\n".join(self._pane_lines) + "\n"
        raise AssertionError(f"unexpected tmux command: {args}")

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self._options.get((pane_id, option), "")

    def list_sessions(self):
        return self._sessions


def _fixture_probe(tmp_path, table, agent_cwds=None) -> MetalProbe:
    projects = tmp_path / "projects"
    project_dir = projects / encode_claude_project_dir("/scratch/a")
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / f"{CLAUDE_SESSION}.jsonl").write_text(
        json.dumps({"sessionId": CLAUDE_SESSION}) + "\n"
    )
    codex_home = tmp_path / "codex"
    day = codex_home / "sessions" / "2026" / "06" / "10"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"rollout-2026-06-10T10-28-09-{CODEX_SESSION}.jsonl").write_text(
        json.dumps({"payload": {"id": CODEX_SESSION, "cwd": "/scratch/b"}}) + "\n"
    )
    agent_cwds = agent_cwds or {}
    return MetalProbe(
        process_table=table,
        process_cwd=lambda pid: agent_cwds.get(pid),
        claude_projects=projects,
        codex_home=codex_home,
    )


# --- session guard -------------------------------------------------------------


def test_refuses_main_outright():
    adapter = _FakeAdapter([])
    with pytest.raises(MetalRestartRefused, match="refuses --session main"):
        assert_legal_session(adapter, "main")


def test_refuses_session_grouped_with_main():
    adapter = _FakeAdapter(
        [],
        sessions=[
            {
                "session_name": "main",
                "session_group": "g0",
                "window_index": "1",
                "window_name": "palace",
            },
            {
                "session_name": "mirror",
                "session_group": "g0",
                "window_index": "1",
                "window_name": "palace",
            },
        ],
    )
    with pytest.raises(MetalRestartRefused, match="session-grouped with main"):
        assert_legal_session(adapter, "mirror")


def test_allows_isolated_sandbox_session():
    adapter = _FakeAdapter([])
    assert_legal_session(adapter, "metalrt")


def test_metal_restart_main_guard_runs_before_observation():
    adapter = _FakeAdapter([])
    with pytest.raises(MetalRestartRefused):
        metal_restart(adapter, "main")
    assert adapter.calls == []  # refused before any tmux read


# --- terminate_agent -----------------------------------------------------------


def test_terminate_agent_sigterm_clean_exit():
    tables = iter([{42: (1, "claude")}, {}])
    kills: list[tuple[int, int]] = []
    ok = terminate_agent(
        42,
        table_reader=lambda: next(tables, {}),
        kill_fn=lambda pid, sig: kills.append((pid, sig)),
        sleep_fn=lambda _s: None,
        timeout_seconds=2,
    )
    assert ok
    assert len(kills) == 1  # no SIGKILL escalation needed


def test_terminate_agent_already_gone():
    def raise_lookup(_pid, _sig):
        raise ProcessLookupError

    assert terminate_agent(42, table_reader=dict, kill_fn=raise_lookup, sleep_fn=lambda _s: None)


# --- metal_restart flow ----------------------------------------------------------


def _three_pane_adapter() -> _FakeAdapter:
    return _FakeAdapter(
        pane_lines=[
            "%1\t1\tmetalrt\t1\tbash\t100\t/scratch/a",
            "%2\t1\tmetalrt\t2\tcodex\t200\t/scratch/b",
            "%3\t1\tmetalrt\t3\tzsh\t300\t/scratch/c",
        ],
        options={
            ("%1", "@PANE_ID"): "metalrt:A",
            ("%2", "@PANE_ID"): "metalrt:B",
            ("%3", "@PANE_ID"): "metalrt:C",
        },
    )


def _three_pane_table() -> dict[int, tuple[int, str]]:
    return {
        100: (1, "zsh"),
        101: (100, "bash /live/cli-tools/scripts/claude-wrapper.sh"),
        102: (101, "/Users/x/.local/bin/claude --resume"),
        200: (1, "zsh"),
        201: (200, "/opt/homebrew/bin/codex"),
        300: (1, "zsh"),
    }


def test_dry_run_resolves_without_killing(tmp_path):
    adapter = _three_pane_adapter()
    table = _three_pane_table()
    probe = _fixture_probe(tmp_path, table, agent_cwds={201: "/scratch/b"})
    kills: list[tuple[int, int]] = []
    result = metal_restart(
        adapter,
        "metalrt",
        dry_run=True,
        probe=probe,
        kill_fn=lambda pid, sig: kills.append((pid, sig)),
        runner=lambda *a, **k: pytest.fail("dry-run must not dispatch"),
    )
    assert kills == []
    statuses = {r.pane_label: r.status for r in result.results}
    assert statuses == {
        "metalrt:A": "would_resume",
        "metalrt:B": "would_resume",
        "metalrt:C": "skipped",
    }
    by_label = {r.pane_label: r for r in result.results}
    assert by_label["metalrt:A"].resume_id == CLAUDE_SESSION
    assert by_label["metalrt:B"].resume_id == CODEX_SESSION
    assert result.ok


def test_execute_kills_then_dispatches_db_free(tmp_path):
    adapter = _three_pane_adapter()
    table = _three_pane_table()
    probe = _fixture_probe(tmp_path, table, agent_cwds={201: "/scratch/b"})
    killed: set[int] = set()

    def kill_fn(pid: int, _sig: int) -> None:
        killed.add(pid)

    def table_reader() -> dict[int, tuple[int, str]]:
        return {pid: row for pid, row in table.items() if pid not in killed}

    dispatched: list[dict] = []

    def runner(argv, *, env, **_kwargs):
        dispatched.append({"argv": argv, "env": env})
        return subprocess.CompletedProcess(argv, 0, stdout="dispatched", stderr="")

    result = metal_restart(
        adapter,
        "metalrt",
        probe=probe,
        runner=runner,
        table_reader=table_reader,
        kill_fn=kill_fn,
        sleep_fn=lambda _s: None,
    )
    assert result.ok
    assert killed == {102, 201}  # the engine binaries, never the pane shells
    assert len(dispatched) == 2
    claude_call, codex_call = dispatched
    assert claude_call["argv"][1:] == [
        "--id",
        CLAUDE_SESSION,
        "--engine",
        "claude",
        "--dir",
        "/scratch/a",
        "--pane",
        "%1",
    ]
    assert codex_call["argv"][1:] == [
        "--id",
        CODEX_SESSION,
        "--engine",
        "codex",
        "--dir",
        "/scratch/b",
        "--pane",
        "%2",
    ]
    for call in dispatched:
        assert not os.path.exists(call["env"]["TOKEN_API_DB"])  # DB lookup is a no-op
    statuses = {r.pane_label: r.status for r in result.results}
    assert statuses == {
        "metalrt:A": "resumed",
        "metalrt:B": "resumed",
        "metalrt:C": "skipped",
    }


def test_dispatch_failure_reported_not_raised(tmp_path):
    adapter = _FakeAdapter(
        pane_lines=["%1\t1\tmetalrt\t1\tbash\t100\t/scratch/a"],
        options={("%1", "@PANE_ID"): "metalrt:A"},
    )
    table = {
        100: (1, "zsh"),
        101: (100, "/Users/x/.local/bin/claude"),
    }
    probe = _fixture_probe(tmp_path, table)
    killed: set[int] = set()

    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 73, stdout="", stderr="target pane occupied")

    result = metal_restart(
        adapter,
        "metalrt",
        probe=probe,
        runner=runner,
        table_reader=lambda: {pid: row for pid, row in table.items() if pid not in killed},
        kill_fn=lambda pid, _sig: killed.add(pid),
        sleep_fn=lambda _s: None,
    )
    assert not result.ok
    assert result.results[0].status == "failed"
    assert "occupied" in result.results[0].detail


def test_unresolvable_agent_pane_is_never_killed(tmp_path):
    # Agent with no transcript: skip and leave the process alone.
    adapter = _FakeAdapter(
        pane_lines=["%1\t1\tmetalrt\t1\tbash\t100\t/scratch/empty"],
        options={("%1", "@PANE_ID"): "metalrt:A"},
    )
    table = {100: (1, "zsh"), 101: (100, "/Users/x/.local/bin/claude")}
    probe = MetalProbe(
        process_table=table,
        process_cwd=lambda _pid: None,
        claude_projects=tmp_path / "projects",
        codex_home=tmp_path / "codex",
    )
    result = metal_restart(
        adapter,
        "metalrt",
        probe=probe,
        runner=lambda *a, **k: pytest.fail("must not dispatch"),
        kill_fn=lambda *_a: pytest.fail("must not kill an unresolvable agent"),
        sleep_fn=lambda _s: None,
    )
    assert result.results[0].status == "skipped"
    assert result.ok


def test_render_shape():
    adapter = _three_pane_adapter()
    rendered = render_metal_restart_result(
        metal_restart(
            adapter,
            "metalrt",
            dry_run=True,
            probe=MetalProbe(
                process_table={},
                process_cwd=lambda _pid: None,
                claude_projects=pathlib.Path("/nonexistent"),
                codex_home=pathlib.Path("/nonexistent"),
            ),
        )
    )
    assert rendered.startswith("metal-restart metalrt (dry-run): 0 resumable, 3 skipped, 0 failed")
    assert "metalrt:A" in rendered


def test_metal_modules_are_statically_db_free():
    # The whole point of the metal path: restore decisions never consult the
    # registry. Enforce it at the import level so a future edit can't quietly
    # re-anchor on the DB or the HTTP registry.
    import ast

    forbidden_modules = {"sqlite3", "urllib", "requests", "http", "socket"}
    forbidden_names = {"instance_registry", "fetch_instance_registry", "api"}
    for module in ("metal_resolver.py", "metal_restart.py"):
        tree = ast.parse((ROOT / "lib" / "tmuxctl" / module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_name = alias.name.split(".")[0]
                    assert root_name not in forbidden_modules, (
                        f"{module} imports forbidden module: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                source_module = (node.module or "").split(".")[-1]
                assert source_module not in forbidden_modules | forbidden_names, (
                    f"{module} imports from forbidden module: {node.module}"
                )
                for alias in node.names:
                    assert alias.name not in forbidden_names, (
                        f"{module} imports forbidden name: {alias.name}"
                    )
