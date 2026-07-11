"""Config and wiring tests for the generic command-boundary guard."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES = REPO_ROOT / "claude-config" / "hooks" / "command-boundary-rules.json"
HOOK = REPO_ROOT / "claude-config" / "hooks" / "command-boundary-guard.sh"
CLAUDE_SETTINGS = REPO_ROOT / "claude-config" / "settings.template.json"
CODEX_HOOKS = REPO_ROOT / "claude-config" / "codex-hooks.template.json"


def test_rules_json_schema_minimums():
    data = json.loads(RULES.read_text())
    assert data["version"] == 1
    assert isinstance(data["rules"], list)
    assert {rule["id"] for rule in data["rules"]} == {
        "direct-gh-pr",
        "runtime-unlock",
        "broad-nas-search",
        "raw-tmux-mutation",
    }
    for rule in data["rules"]:
        assert rule["id"]
        assert isinstance(rule.get("matcher"), dict)
        assert rule["matcher"].get("type")
        assert rule.get("deny", {}).get("reason")
        assert rule.get("deny", {}).get("redirect")


def test_deny_json_shape_matches_pretooluse():
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh pr view"}})
    proc = subprocess.run(
        ["bash", str(HOOK)], input=payload, capture_output=True, text=True, timeout=15
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert set(out) == {"hookSpecificOutput"}
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "permissionDecisionReason" in hso
    assert "The user has set" in hso["permissionDecisionReason"]


def test_claude_settings_uses_single_boundary_guard_for_these_rules():
    settings = json.loads(CLAUDE_SETTINGS.read_text())
    commands = [
        hook["command"]
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "Bash"
        for hook in entry["hooks"]
        if hook.get("type") == "command"
    ]
    boundary = [cmd for cmd in commands if "command-boundary-guard.sh" in cmd]
    assert boundary == ["bash ~/.claude/hooks/command-boundary-guard.sh"]
    # Boundary guard must run before the generic Token-API hook so config-level
    # redirects (notably broad NAS search -> nas-grep) are not preempted by
    # older server-side hard-deny messages.
    assert commands.index("bash ~/.claude/hooks/command-boundary-guard.sh") < commands.index(
        "HOOK_DEBUG=1 HOOK_ACTION_TYPE=PreToolUse bash ~/.claude/hooks/generic-hook.sh"
    )
    assert not any("runtime-unlock-guard.sh" in cmd for cmd in commands)
    assert not any("pr-gh-guard.sh" in cmd for cmd in commands)


def test_codex_hooks_template_uses_same_live_runtime_guard_and_config():
    hooks = json.loads(CODEX_HOOKS.read_text())
    commands = [
        hook["command"]
        for entry in hooks["hooks"]["PreToolUse"]
        for hook in entry["hooks"]
        if hook.get("type") == "command"
    ]
    assert len(commands) == 1
    command = commands[0]
    assert (
        "${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/claude-config/hooks/command-boundary-guard.sh"
        in command
    )
    assert (
        "${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/claude-config/hooks/command-boundary-rules.json"
        in command
    )
    assert "runtime-unlock-guard.sh" not in command
    assert "pr-gh-guard.sh" not in command
