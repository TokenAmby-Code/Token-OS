from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "tmux-plan-prefix"


def _dry(state: str, agent: str | None = None) -> str:
    cmd = [str(SCRIPT), "--pane", "%1", "--dry-run", "--state", state]
    if agent:
        cmd += ["--agent", agent]
    return subprocess.check_output(cmd, text=True).strip()


def test_shift_tab_cycle_dry_run_none_to_preplan_claude():
    out = _dry("none")
    assert "agent=claude" in out
    assert "planning_state=preplanning" in out
    assert "insert:/preplan" in out
    assert "subscribe:preplan_plan" in out


def test_shift_tab_cycle_dry_run_none_to_preplan_codex_explicit_skill():
    out = _dry("none", "codex")
    assert "agent=codex" in out
    assert "planning_state=preplanning" in out
    assert "insert:$preplan" in out
    assert "subscribe:preplan_plan" in out


def test_shift_tab_cycle_dry_run_preplan_to_plan_claude():
    out = _dry("preplanning")
    assert "planning_state=planning" in out
    assert "mutate:/preplan->/plan" in out


def test_shift_tab_cycle_dry_run_preplan_to_plan_codex():
    out = _dry("preplanning", "codex")
    assert "planning_state=planning" in out
    assert "mutate:$preplan->/plan" in out


def test_shift_tab_cycle_dry_run_plan_to_cleared():
    out = _dry("planning")
    assert "planning_state=none" in out
    assert "clear:/plan" in out
    assert "unsubscribe:preplan_plan" in out
