"""Current-behavior pins for Coherence Map §4 drift pairs.

Each test intentionally asserts what the code does *today*, even where the vault
records a different future direction. The matching future assertions live in the
advisory bounty-board lane and should xfail until that future behavior ships.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import occupancy
from tmuxctl.metal_resolver import MetalProbe, encode_claude_project_dir
from tmuxctl.metal_restart import metal_restart
from tmuxctl.occupancy import assert_dispatch_target_available
from tmuxctl.pane_select import select_pane
from tmuxctl.public_ids import physical_to_public_id_map
from tmuxctl.resolver import resolve_instance

CLAUDE_SESSION = "a4383c8b-5e2d-461a-8658-d96ff6db1141"


class _DisplayAdapter:
    def __init__(self, row: str):
        self.row = row

    def _resolve_pane_target_arg(self, pane: str) -> str:
        return pane

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[0] == "display-message":
            return self.row
        raise AssertionError(args)


def test_pin_current_occupancy_empty_non_singleton_worker_is_dispatch_available(monkeypatch):
    """Current behavior: no stamp/process/singleton signal means dispatch-available.

    Future bounty counterpart: a persisted daemon ledger SHIPPED/OPEN row should
    make this same empty-looking pane occupied.
    """

    monkeypatch.setattr(occupancy, "_active_agent", lambda _pid: False)
    adapter = _DisplayAdapter("%80\t\tmechanicus:8\tmechanicus\t1000")

    result = assert_dispatch_target_available(adapter, "%80")

    assert result.dispatch_available is True
    assert result.instance_id == ""
    assert result.live_agent is False


class _ResolveInstanceAdapter:
    def __init__(self, rows: list[tuple[str, str, str]]):
        self.rows = rows

    def run(self, *args: str, allow_failure: bool = False) -> str:
        assert args[:2] == ("list-panes", "-a")
        return "\n".join("\t".join(row) for row in self.rows) + "\n"


def test_pin_current_resolve_instance_is_stamp_only_and_skips_unstamped_panes():
    """Current behavior: canonical ids are not resolved without matching @INSTANCE_ID."""

    adapter = _ResolveInstanceAdapter([("%81", "", "mechanicus:9")])

    resolved = resolve_instance(adapter, "iid-known-to-token-api")

    assert resolved.found is False
    assert resolved.pane_id is None


def test_pin_current_metal_restart_invokes_db_free_dispatch_cli_not_tmuxctld_ship(tmp_path):
    """Current behavior: metal restart resumes via bin/dispatch --pane after killing engines."""

    projects = tmp_path / "projects"
    project_dir = projects / encode_claude_project_dir("/scratch/a")
    project_dir.mkdir(parents=True)
    (project_dir / f"{CLAUDE_SESSION}.jsonl").write_text(
        json.dumps({"sessionId": CLAUDE_SESSION}) + "\n"
    )
    codex_home = tmp_path / "codex"
    table = {
        100: (1, "zsh"),
        101: (100, "bash /live/cli-tools/scripts/agent-wrapper.sh claude"),
        102: (101, "/Users/x/.local/bin/claude --resume"),
    }
    killed: set[int] = set()

    class Adapter:
        def run(self, *args: str, **_kwargs) -> str:
            if args[0] == "list-panes":
                return "%82\t1\tmetalrt\t1\tbash\t100\t/scratch/a\n"
            raise AssertionError(args)

        def show_pane_option(self, pane_id: str, option: str) -> str:
            return "metalrt:A" if option == "@PANE_ID" else ""

        def list_sessions(self):
            return [
                {
                    "session_name": "metalrt",
                    "session_group": "",
                    "window_index": "1",
                    "window_name": "metalrt",
                }
            ]

    dispatched: list[dict[str, object]] = []

    def runner(argv, *, env, **_kwargs):
        dispatched.append({"argv": argv, "env": env})
        return subprocess.CompletedProcess(argv, 0, stdout="dispatched", stderr="")

    result = metal_restart(
        Adapter(),
        "metalrt",
        probe=MetalProbe(
            process_table=table,
            process_cwd=lambda _pid: None,
            claude_projects=projects,
            codex_home=codex_home,
        ),
        runner=runner,
        table_reader=lambda: {pid: row for pid, row in table.items() if pid not in killed},
        kill_fn=lambda pid, _sig: killed.add(pid),
        sleep_fn=lambda _seconds: None,
    )

    assert result.ok
    assert dispatched
    argv = dispatched[0]["argv"]
    assert pathlib.Path(argv[0]).name == "dispatch"
    assert argv[1:] == [
        "--id",
        CLAUDE_SESSION,
        "--engine",
        "claude",
        "--dir",
        "/scratch/a",
        "--pane",
        "%82",
    ]
    assert not os.path.exists(dispatched[0]["env"]["TOKEN_API_DB"])


class _PublicIdAdapter:
    def run(self, *args: str, allow_failure: bool = False) -> str:
        assert args[:4] == ("list-panes", "-a", "-F", "#{pane_id}\t#{@PANE_ID}")
        return "%83\tmechanicus:10\n%84\t\n"


def test_pin_current_public_identity_map_comes_from_live_pane_id_stamps_only():
    """Current behavior: panes missing @PANE_ID are omitted from public-id mapping."""

    assert physical_to_public_id_map(_PublicIdAdapter()) == {"%83": "mechanicus:10"}


def test_pin_current_empty_stamp_singleton_is_refused_by_label_guard(monkeypatch):
    """Current behavior: singleton labels are protected even when @INSTANCE_ID is empty."""

    monkeypatch.setattr(occupancy, "_active_agent", lambda _pid: False)
    adapter = _DisplayAdapter("%85\t\tlegion:custodes\tlegion\t1000")

    with pytest.raises(ValueError, match="protected singleton"):
        assert_dispatch_target_available(adapter, "%85")


class _PaneSelectAdapter:
    def __init__(self):
        self.current = "%N"
        self.global_options: dict[str, str] = {}
        self.commands: list[tuple[str, ...]] = []
        self.panes = {
            "%N": {"pane_id": "%N", "role": "palace:N"},
            "%E": {"pane_id": "%E", "role": "palace:E"},
            "palace:E": {"pane_id": "%E", "role": "palace:E"},
        }

    def _pane(self, target: str | None = None):
        return self.panes[target or self.current]

    def show_pane_option(self, pane_id: str, option: str) -> str:
        if option == "@PANE_ID":
            return self._pane(pane_id)["role"]
        return ""

    def show_window_option(self, target: str, option: str) -> str:
        return ""

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        if args[0] == "set-option" and "-g" in args:
            self.global_options[args[-2]] = args[-1]
            return ""
        if args[0] == "display-message":
            fmt = args[-1]
            if fmt == "#{pane_id}\t#{session_name}\t#{window_index}\t#{window_name}":
                return f"{self.current}\tmain\t1\tpalace\n"
            if fmt == (
                "#{pane_id}\t#{session_name}\t#{window_index}\t#{window_name}"
                "\t#{window_zoomed_flag}"
            ):
                return f"{self.current}\tmain\t1\tpalace\t0\n"
            if fmt == "#{window_zoomed_flag}":
                return "0\n"
            if fmt == "#{pane_id}":
                return f"{self.current}\n"
        if args[0] == "list-panes":
            return "%N\tpalace:N\t\t0\t0\n%E\tpalace:E\t\t80\t0\n"
        if args[0] == "select-pane":
            self.current = "%E"
            return ""
        return ""


def test_pin_current_pane_select_directly_sets_tmux_focus_allow_and_selects_pane():
    """Current behavior: pane-select mutates tmux focus/global options directly."""

    adapter = _PaneSelectAdapter()

    result = select_pane(adapter, mode="absolute", direction="right", client="/dev/ttys001")

    assert result.endswith("palace:E")
    assert adapter.global_options["@IMPERIUM_HUMAN_MECHANICUS_FOCUS_CLIENT"] == "/dev/ttys001"
    assert ("select-pane", "-t", "palace:E") in adapter.commands


def test_pin_current_programmatic_pure_pane_id_read_returns_raw_physical_id(tmp_path):
    """Current behavior: pure programmatic pane-id reads expose raw %pane for round-trip."""

    fake_tmux = tmp_path / "real-tmux"
    fake_tmux.write_text(
        textwrap.dedent(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${1:-}" == "list-panes" && "${2:-}" == "-a" && "${3:-}" == "-F" && "${4:-}" == $'#{pane_id}\t#{@PANE_ID}' ]]; then
              printf '%%86\tpalace:N\n'
              exit 0
            fi
            if [[ "$*" == "display-message -p #{pane_id}" ]]; then
              printf '%%86\n'
              exit 0
            fi
            printf 'unexpected args: %s\n' "$*" >&2
            exit 64
            """
        ).strip()
        + "\n"
    )
    fake_tmux.chmod(0o755)
    env = {
        **os.environ,
        "IMPERIUM_TMUX_BIN": str(fake_tmux),
        "IMPERIUM_ALLOW_TMUX_FOCUS": "1",
        "IMPERIUM_ALLOW_MECHANICUS_FOCUS": "1",
    }
    env.pop("IMPERIUM_TMUX_SANITIZE_IDS", None)
    env.pop("IMPERIUM_TMUX_RAW", None)

    proc = subprocess.run(
        [str(ROOT / "bin" / "tmux"), "display-message", "-p", "#{pane_id}"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "%86\n"
