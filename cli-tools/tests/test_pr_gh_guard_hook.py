"""Tests for the PreToolUse direct gh-pr guard.

Agents use the `pr` skill / `pr-step` for PR lifecycle work. This hook denies
command-position `gh pr ...` while allowing `pr-step`, non-PR `gh` commands, and
commands that merely mention the text "gh pr" as an argument.
"""

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "claude-config" / "hooks" / "pr-gh-guard.sh"


def run_hook(command: str):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
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
# DENY: direct command-position gh-pr invocations.
# --------------------------------------------------------------------------- #


def test_gh_pr_view_is_denied():
    reason = assert_denied(run_hook("gh pr view"))
    assert "`pr` skill" in reason
    assert "`pr-step`" in reason


def test_gh_pr_after_and_separator_is_denied():
    assert_denied(run_hook("cd repo && gh pr create"))


def test_gh_pr_after_env_prefix_is_denied():
    assert_denied(run_hook("GH_TOKEN=x gh pr merge 123"))


def test_gh_pr_after_newline_is_denied():
    assert_denied(run_hook("pwd\ngh pr checks"))


# --------------------------------------------------------------------------- #
# ALLOW: canonical workflow and non-PR/text-search commands.
# --------------------------------------------------------------------------- #


def test_pr_step_is_allowed():
    assert_allowed(run_hook("pr-step"))


def test_pr_step_force_review_is_allowed():
    assert_allowed(run_hook("pr-step --force review"))


def test_grep_for_gh_pr_text_is_allowed():
    assert_allowed(run_hook('grep -R "gh pr" docs'))


def test_non_pr_gh_command_is_allowed():
    assert_allowed(run_hook("gh issue view 123"))
