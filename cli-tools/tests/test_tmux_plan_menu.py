from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "tmux-plan-menu"


def _dry(selection: str, agent: str | None = None) -> str:
    cmd = [str(SCRIPT), "--pane", "%1", "--dry-run", "--selection", selection]
    if agent:
        cmd += ["--agent", agent]
    return subprocess.check_output(cmd, text=True).strip()


def test_preplan_claude_sends_slash_leader_and_subscribes():
    out = _dry("preplan", "claude")
    assert "selection=preplan" in out
    assert "agent=claude" in out
    assert "send:/preplan" in out
    assert "subscribe:preplan_plan" in out
    assert "state:preplanning" in out


def test_preplan_codex_sends_dollar_leader():
    out = _dry("preplan", "codex")
    assert "agent=codex" in out
    assert "send:$preplan" in out
    assert "subscribe:preplan_plan" in out


def test_plan_sends_universal_slash_plan():
    out = _dry("plan")
    assert "selection=plan" in out
    assert "send:/plan" in out
    assert "state:planning" in out


def test_compact_sends_universal_slash_compact():
    out = _dry("compact")
    assert "selection=compact" in out
    assert "send:/compact" in out


def test_shift_tab_forwards_literal_btab():
    out = _dry("shift+tab")
    assert "selection=shift+tab" in out
    assert "send-keys:BTab" in out


def test_cancel_is_noop():
    out = _dry("cancel")
    assert "selection=cancel" in out
    assert "noop" in out
