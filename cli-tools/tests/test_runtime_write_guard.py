"""Tests for the PreToolUse runtime-write-guard hook.

The local-runtime cutover reintroduced the "dancing on main" failure mode in a
new shape: agents editing the deploy-owned ~/runtimes/Token-OS/live (or askCivic)
checkout directly instead of in a worktree. This PreToolUse hook denies writes
that land under a runtime root, for BOTH harnesses (Claude Code and Codex emit
the same wire contract). Allow = exit 0 with empty stdout; deny = a
hookSpecificOutput JSON with permissionDecision "deny".

The bar: hard-block the edit tools (Write/Edit/MultiEdit/NotebookEdit, Codex
apply_patch) and precise in-place bash mutations into a runtime path, while
NEVER blocking worktree work, vault edits, reads, copy-out, or the deploy path
(token-restart).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "cli-tools" / "scripts" / "runtime-write-guard.sh"

HOME = os.path.expanduser("~")
RT = f"{HOME}/runtimes/Token-OS/live"  # a deploy-owned runtime checkout
WT = f"{HOME}/worktrees/Token-OS/wt-x"  # a legitimate work target
VAULT = "/Volumes/Imperium/Imperium-ENV"


def run_hook(payload: dict, env_extra: dict | None = None):
    env = dict(os.environ)
    # Never inherit the escape hatch from the host/runner — otherwise a stray
    # IMPERIUM_ALLOW_RUNTIME_WRITE=1 in the environment would silently turn every
    # deny-path assertion into an allow. Only explicit env_extra may set it.
    env.pop("IMPERIUM_ALLOW_RUNTIME_WRITE", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    return proc


def is_deny(proc) -> bool:
    assert proc.returncode == 0, f"hook must always exit 0; got {proc.returncode}"
    out = proc.stdout.strip()
    if not out:
        return False
    data = json.loads(out)
    return data.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


# --- DENY cases: writes that land in a runtime checkout --------------------

DENY_CASES = [
    (
        "write_into_live",
        {"tool_name": "Write", "tool_input": {"file_path": f"{RT}/token-api/app.py"}},
    ),
    (
        "edit_into_askcivic",
        {"tool_name": "Edit", "tool_input": {"file_path": f"{HOME}/runtimes/askCivic/x.py"}},
    ),
    ("multiedit_into_live", {"tool_name": "MultiEdit", "tool_input": {"file_path": f"{RT}/a.py"}}),
    (
        "notebook_into_live",
        {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": f"{RT}/n.ipynb"}},
    ),
    (
        "relative_path_in_live_cwd",
        {"tool_name": "Edit", "cwd": f"{RT}/token-api", "tool_input": {"file_path": "app.py"}},
    ),
    (
        "codex_apply_patch_tool",
        {
            "tool_name": "apply_patch",
            "cwd": RT,
            "tool_input": {
                "input": "*** Begin Patch\n*** Update File: token-api/app.py\n*** End Patch"
            },
        },
    ),
    (
        "codex_apply_patch_namespaced",
        {
            "tool_name": "functions.apply_patch",
            "cwd": RT,
            "tool_input": {"input": "*** Begin Patch\n*** Add File: new.py\n*** End Patch"},
        },
    ),
    ("bash_redirect", {"tool_name": "Bash", "tool_input": {"command": f"echo hi > {RT}/foo.txt"}}),
    (
        "bash_append_tokenos_var",
        {"tool_name": "Bash", "tool_input": {"command": "echo x >> $TOKEN_OS/token-api/app.py"}},
    ),
    (
        "bash_tilde_redirect",
        {"tool_name": "Bash", "tool_input": {"command": "echo x > ~/runtimes/Token-OS/live/z"}},
    ),
    (
        "bash_sed_inplace",
        {"tool_name": "Bash", "tool_input": {"command": f"sed -i '' s/a/b/ {RT}/app.py"}},
    ),
    ("bash_tee", {"tool_name": "Bash", "tool_input": {"command": f"echo x | tee {RT}/y"}}),
    ("bash_rm", {"tool_name": "Bash", "tool_input": {"command": f"rm -rf {RT}/token-api/old"}}),
    ("bash_cp_dest", {"tool_name": "Bash", "tool_input": {"command": f"cp /tmp/a.py {RT}/a.py"}}),
    (
        "bash_apply_patch_heredoc",
        {
            "tool_name": "Bash",
            "cwd": RT,
            "tool_input": {
                "command": "apply_patch <<EOF\n*** Begin Patch\n*** Update File: token-api/app.py\n*** End Patch\nEOF"
            },
        },
    ),
    (
        "bash_git_tree_write",
        {"tool_name": "Bash", "tool_input": {"command": f"git -C {RT} reset --hard origin/main"}},
    ),
    (
        "codex_command_array",
        {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", f"echo x > {RT}/z"]}},
    ),
    (
        "nas_mirror_write",
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "/Volumes/Imperium/runtimes/token-os/live/x.py"},
        },
    ),
    # git write subcommand reachable past intervening options.
    (
        "bash_git_tree_write_with_opts",
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": f"git -C {RT} -c advice.detachedHead=false reset --hard origin/main"
            },
        },
    ),
    # Concrete /Users/<other> home must still trip the guard even if it isn't
    # this hook's own $HOME.
    (
        "bash_redirect_other_user_home",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo x > /Users/someone/runtimes/Token-OS/live/z"},
        },
    ),
    # The escape-hatch token only counts as a real leading assignment — here it
    # is just an echo argument, so the redirect must still be denied.
    (
        "bash_fake_escape_hatch_does_not_bypass",
        {
            "tool_name": "Bash",
            "tool_input": {"command": f"echo IMPERIUM_ALLOW_RUNTIME_WRITE=1; echo x > {RT}/z"},
        },
    ),
    # An escape-hatch assignment in a LATER segment must not green-light a
    # runtime write in an earlier segment — only a true leading ^ assignment.
    (
        "bash_escape_hatch_later_segment_does_not_bypass",
        {
            "tool_name": "Bash",
            "tool_input": {"command": f"echo x > {RT}/z ; IMPERIUM_ALLOW_RUNTIME_WRITE=1 true"},
        },
    ),
    # git option BEFORE the -C flag must not evade the direct-git-write detector.
    (
        "bash_git_opt_before_C",
        {
            "tool_name": "Bash",
            "tool_input": {"command": f"git -c core.fsmonitor=false -C {RT} reset --hard"},
        },
    ),
]


@pytest.mark.parametrize("label,payload", DENY_CASES, ids=[c[0] for c in DENY_CASES])
def test_denies_runtime_writes(label, payload):
    proc = run_hook(payload)
    assert is_deny(proc), f"{label} should be DENIED\nstdout={proc.stdout!r}"
    # The deny reason must steer toward a worktree.
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "worktree" in reason.lower()


# --- ALLOW cases: legitimate work must never be blocked --------------------

ALLOW_CASES = [
    (
        "write_into_worktree",
        {"tool_name": "Write", "tool_input": {"file_path": f"{WT}/token-api/app.py"}},
    ),
    ("edit_vault", {"tool_name": "Edit", "tool_input": {"file_path": f"{VAULT}/CLAUDE.md"}}),
    (
        "token_restart_deploy",
        {"tool_name": "Bash", "tool_input": {"command": "token-restart --sync"}},
    ),
    ("cat_read_from_live", {"tool_name": "Bash", "tool_input": {"command": f"cat {RT}/AGENTS.md"}}),
    (
        "grep_read_in_live",
        {"tool_name": "Bash", "tool_input": {"command": f"rg foo {RT}/token-api"}},
    ),
    (
        "cp_out_of_live",
        {"tool_name": "Bash", "tool_input": {"command": f"cp {RT}/app.py /tmp/app.py"}},
    ),
    (
        "git_read_in_live",
        {"tool_name": "Bash", "tool_input": {"command": f"git -C {RT} log --oneline -5"}},
    ),
    ("git_status_in_live", {"tool_name": "Bash", "tool_input": {"command": f"git -C {RT} status"}}),
    (
        "git_commit_in_worktree",
        {"tool_name": "Bash", "tool_input": {"command": f"git -C {WT} commit -am x"}},
    ),
    (
        "redirect_into_worktree",
        {"tool_name": "Bash", "tool_input": {"command": f"echo x > {WT}/z"}},
    ),
    ("source_live_env", {"tool_name": "Bash", "tool_input": {"command": f"source {RT}/.env"}}),
    ("empty_payload", {}),
]


@pytest.mark.parametrize("label,payload", ALLOW_CASES, ids=[c[0] for c in ALLOW_CASES])
def test_allows_legitimate_work(label, payload):
    proc = run_hook(payload)
    assert not is_deny(proc), f"{label} should be ALLOWED\nstdout={proc.stdout!r}"


def test_malformed_input_fails_open():
    proc = subprocess.run(
        ["bash", str(HOOK)],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_env_escape_hatch_allows():
    payload = {"tool_name": "Write", "tool_input": {"file_path": f"{RT}/x.py"}}
    proc = run_hook(payload, env_extra={"IMPERIUM_ALLOW_RUNTIME_WRITE": "1"})
    assert not is_deny(proc)


def test_inline_escape_hatch_allows():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"IMPERIUM_ALLOW_RUNTIME_WRITE=1 echo x > {RT}/z"},
    }
    proc = run_hook(payload)
    assert not is_deny(proc)
