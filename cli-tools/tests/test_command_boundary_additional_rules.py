"""Behavior tests for non-PR generic command-boundary rules."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "claude-config" / "hooks" / "command-boundary-guard.sh"


def run_hook(command: str, cwd: str | None = None):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd:
        payload["cwd"] = cwd
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )


def assert_denied(proc):
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


def assert_allowed(proc):
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --------------------------------------------------------------------------- #
# Broad NAS search boundary.
# --------------------------------------------------------------------------- #


def test_find_volumes_root_scan_redirects_to_nas_grep():
    reason = assert_denied(run_hook("find /Volumes -type d -name token-api"))
    assert "NAS search" in reason
    assert "nas-grep" in reason


def test_rg_imperium_root_scan_redirects_to_nas_grep():
    assert_denied(run_hook("rg foo /Volumes/Imperium"))


def test_cd_into_imperium_then_relative_rg_is_denied():
    assert_denied(run_hook("cd /Volumes/Imperium && rg foo ."))


def test_ugrep_mnt_imperium_root_scan_is_denied():
    assert_denied(run_hook("ugrep foo /mnt/imperium"))


def test_recursive_grep_civic_root_scan_is_denied():
    assert_denied(run_hook("grep -R foo /Volumes/Civic"))


def test_nonrecursive_grep_broad_root_is_allowed():
    assert_allowed(run_hook("grep foo /Volumes/Imperium"))


def test_rg_inside_nas_subdirectory_is_denied():
    assert_denied(run_hook("rg foo $IMPERIUM/Imperium-ENV"))


def test_relative_rg_from_nas_subdirectory_is_denied():
    assert_denied(run_hook("rg foo .", cwd="/Volumes/Imperium/Imperium-ENV"))


def test_local_rg_is_allowed():
    assert_allowed(
        run_hook("rg foo .", cwd="/Users/tokenclaw/worktrees/Token-OS/wt-command-boundary-guards")
    )


def test_local_recursive_grep_is_allowed():
    assert_allowed(
        run_hook(
            "grep -R foo .", cwd="/Users/tokenclaw/worktrees/Token-OS/wt-command-boundary-guards"
        )
    )


def test_local_ugrep_is_allowed():
    assert_allowed(
        run_hook(
            "ugrep foo .", cwd="/Users/tokenclaw/worktrees/Token-OS/wt-command-boundary-guards"
        )
    )


def test_broad_nas_path_as_rg_pattern_is_allowed():
    assert_allowed(run_hook('rg "/Volumes/Imperium" .'))


def test_git_grep_is_allowed():
    assert_allowed(run_hook("git grep foo"))


# --------------------------------------------------------------------------- #
# Raw tmux mutation boundary.
# --------------------------------------------------------------------------- #


def test_raw_tmux_send_keys_is_denied():
    reason = assert_denied(run_hook('tmux send-keys -t 1:S "hello" Enter'))
    assert "pane-control boundary" in reason
    assert "agent-cmd" in reason


def test_raw_tmux_split_window_is_denied():
    assert_denied(run_hook("tmux split-window -h"))


def test_readonly_tmux_capture_pane_is_allowed():
    assert_allowed(run_hook("tmux capture-pane -pt 1:S -S -20"))


def test_tmuxctl_wrapper_is_allowed():
    assert_allowed(run_hook("tmuxctl stack status"))


def test_agent_cmd_wrapper_is_allowed():
    assert_allowed(run_hook("agent-cmd 1:S 'status'"))
