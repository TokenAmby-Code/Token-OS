from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "tmux-plan-menu"


def _dry(
    selection: str,
    agent: str | None = None,
    *,
    pane: str = "%1",
    env: dict[str, str] | None = None,
) -> str:
    cmd = [str(SCRIPT), "--pane", pane, "--dry-run", "--selection", selection]
    if agent:
        cmd += ["--agent", agent]
    return subprocess.check_output(cmd, text=True, timeout=10, env=env).strip()


def test_preplan_claude_inserts_slash_leader_and_subscribes() -> None:
    out = _dry("preplan", "claude")
    assert "selection=preplan" in out
    assert "agent=claude" in out
    assert "insert:/preplan" in out
    assert "subscribe:preplan_plan" in out
    assert "state:preplanning" in out


def test_preplan_codex_inserts_dollar_leader() -> None:
    out = _dry("preplan", "codex")
    assert "agent=codex" in out
    assert "insert:$preplan" in out
    assert "subscribe:preplan_plan" in out


def test_plan_inserts_universal_slash_plan() -> None:
    out = _dry("plan")
    assert "selection=plan" in out
    assert "insert:/plan" in out
    assert "state:planning" in out


def test_compact_inserts_universal_slash_compact() -> None:
    out = _dry("compact")
    assert "selection=compact" in out
    assert "insert:/compact" in out


def test_shift_tab_forwards_literal_btab() -> None:
    out = _dry("shift+tab")
    assert "selection=shift+tab" in out
    assert "send-keys:BTab" in out


def test_cancel_is_noop() -> None:
    out = _dry("cancel")
    assert "selection=cancel" in out
    assert "noop" in out


def test_literal_pane_arg_falls_back_to_btab_pane_env() -> None:
    # display-popup passes the binding's #{pane_id} literally (it does not expand
    # the shell-command), so a non-%id --pane must be ignored and the BTAB_PANE
    # env (expanded at key dispatch) used to lock the invoking pane instead.
    env = {**os.environ, "BTAB_PANE": "%7"}
    out = _dry("plan", pane="#{pane_id}", env=env)
    assert "pane=%7" in out
