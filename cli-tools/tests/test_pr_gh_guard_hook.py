"""Tests for the PreToolUse direct gh-pr guard.

Agents use the `pr` skill / `pr-step` for PR lifecycle writes. This hook denies
write-position `gh pr <write-subcommand> ...` while allowing read-only `gh pr`,
read-only `gh run`, `pr-step`, non-PR `gh` commands, and commands that merely
mention the text "gh pr" as an argument.
"""

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "claude-config" / "hooks" / "command-boundary-guard.sh"

GH_PR_READ_COMMANDS = [
    "gh pr view",
    "gh pr list",
    "gh pr status",
    "gh pr checks",
    "gh pr diff",
]
GH_RUN_READ_COMMANDS = ["gh run view", "gh run list"]
GH_PR_WRITE_SUBCOMMANDS = [
    "create",
    "merge",
    "close",
    "edit",
    "ready",
    "reopen",
    "comment",
    "review",
    "lock",
    "unlock",
]


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
# DENY: direct command-position gh-pr write invocations.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("subcommand", GH_PR_WRITE_SUBCOMMANDS)
def test_gh_pr_write_subcommands_are_denied(subcommand):
    reason = assert_denied(run_hook(f"gh pr {subcommand}"))
    assert "`pr` skill" in reason
    assert "`pr-step`" in reason
    assert "`/pr`" in reason
    assert "self-sufficient since #598" in reason


def test_gh_pr_write_after_and_separator_is_denied():
    assert_denied(run_hook("cd repo && gh pr create"))


def test_gh_pr_write_after_env_prefix_is_denied():
    assert_denied(run_hook("GH_TOKEN=x gh pr merge 123"))


def test_gh_pr_write_after_newline_is_denied():
    assert_denied(run_hook("pwd\ngh pr comment 123 --body ok"))


# --------------------------------------------------------------------------- #
# ALLOW: read-only gh commands, canonical workflow, non-PR/text-search commands.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("command", GH_PR_READ_COMMANDS)
def test_gh_pr_read_commands_are_allowed(command):
    assert_allowed(run_hook(command))


@pytest.mark.parametrize("command", GH_RUN_READ_COMMANDS)
def test_gh_run_read_commands_are_allowed(command):
    assert_allowed(run_hook(command))


def test_pr_step_is_allowed():
    assert_allowed(run_hook("pr-step"))


def test_pr_step_force_review_is_allowed():
    assert_allowed(run_hook("pr-step --force review"))


def test_grep_for_gh_pr_text_is_allowed():
    assert_allowed(run_hook('grep -R "gh pr" docs'))


def test_non_pr_gh_command_is_allowed():
    assert_allowed(run_hook("gh issue view 123"))
