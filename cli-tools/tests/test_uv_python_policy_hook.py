"""Tests for the PreToolUse uv-python-policy companion hook.

The `bin/python` shim is a pure interpreter delegate and deliberately no longer
forces `uv run` (that caused python->uv->python recursion). uv-backed-python
*policy* moved here: this PreToolUse(Bash) hook detects bare `python`/`python3`
invocations and surfaces a NON-BLOCKING advisory steering toward `uv run`, so
the salvage does not silently drop uv enforcement.

Non-blocking by design: the hook always allows the call (permissionDecision
"allow") and exits 0 — it must never break the fleet the way the recursion did.
"""

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "claude-config" / "hooks" / "uv-python-policy.sh"


def run_hook(command: str):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    proc = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return proc


def test_bare_python_fires_advisory(tmp_path):
    proc = run_hook("python script.py")
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    # Advisory only — the call is allowed, never blocked.
    assert hso["permissionDecision"] == "allow"
    assert "uv run" in hso["additionalContext"]


def test_bare_python3_fires_advisory():
    proc = run_hook("python3 -m pytest -q")
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "uv run" in out["hookSpecificOutput"]["additionalContext"]


def test_chained_bare_python_fires_advisory():
    proc = run_hook("cd /tmp && python do_thing.py")
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert "uv run" in out["hookSpecificOutput"]["additionalContext"]


def test_uv_run_python_is_silent():
    proc = run_hook("uv run python script.py")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_no_python_is_silent():
    proc = run_hook("ls -la && git status")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_path_qualified_python_is_silent():
    """Explicit interpreter paths (venv/system) are already deliberate — no nag."""
    proc = run_hook(".venv/bin/python script.py")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_ipython_is_not_a_false_positive():
    proc = run_hook("ipython")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_explicit_raw_bypass_is_silent():
    proc = run_hook("IMPERIUM_PYTHON_RAW=1 python bootstrap.py")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
