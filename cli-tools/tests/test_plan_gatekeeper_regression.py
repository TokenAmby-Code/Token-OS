from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "claude-config" / "hooks" / "plan-gatekeeper.sh"


def test_plan_gatekeeper_no_reject_once_bounce_state_machine():
    text = SCRIPT.read_text()
    assert "claude-plan-bounced" not in text
    assert 'behavior":"deny' not in text
    assert "tmux-plan-approve-clear" in text
