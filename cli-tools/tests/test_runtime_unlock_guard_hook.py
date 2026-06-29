"""Tests for the PreToolUse runtime-unlock-guard hook.

The Token-OS live runtime (~/runtimes/Token-OS/live) is write-locked (cleared
write bits + chflags uchg). That lock is owner-bypassable — an agent runs as the
owning uid — so a determined agent can `chflags nouchg` + `chmod u+w` and edit in
place (it has happened). This PreToolUse(Bash) hook keys on those UNLOCK COMMAND
PATTERNS: it fires ONLY when a command tries to clear the immutable flag or add
write to a runtime path, and DENIES with a verbose educational message. It is a
speed bump with teeth, not a wall; the deploy path uses IMPERIUM_ALLOW_RUNTIME_WRITE.

These tests are pure subprocess invocations of the shell hook — they construct
the PreToolUse JSON envelope and read the verdict off stdout. They never touch
live tmux, the runtime checkout, or any filesystem mode/flag: nothing is chmod'd
or chflag'd, only the hook's *decision* about a command string is asserted.
"""

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "claude-config" / "hooks" / "runtime-unlock-guard.sh"


def run_hook(command: str, env: dict | None = None):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    proc = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    return proc


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
# DENY: agent-origin unlock commands targeting the runtime.
# --------------------------------------------------------------------------- #


def test_chflags_nouchg_runtime_is_denied():
    reason = assert_denied(run_hook("chflags nouchg ~/runtimes/Token-OS/live/token-api/foo.py"))
    # The refusal must TEACH, not read as a bare error.
    assert "write-locked" in reason
    assert "worktree" in reason
    assert "PR" in reason
    assert "--force" in reason
    assert "sanctioned admin/runtime maintenance" in reason
    assert "Do not use force to make a quick code change" in reason


def test_chmod_recursive_u_plus_w_runtime_is_denied():
    assert_denied(run_hook("chmod -R u+w ~/runtimes/Token-OS/live"))


def test_chmod_plus_w_absolute_runtime_is_denied():
    assert_denied(run_hook("chmod +w /Users/tokenclaw/runtimes/Token-OS/live/x"))


def test_chmod_a_plus_w_runtime_is_denied():
    assert_denied(run_hook("chmod a+w ~/runtimes/Token-OS/live/x"))


def test_chmod_u_plus_rwx_satellite_path_is_denied():
    assert_denied(run_hook("chmod -R u+rwx /home/token/runtimes/token-os/live"))


def test_token_os_var_is_denied():
    # $TOKEN_OS resolves to the live checkout without the literal substring.
    assert_denied(run_hook('chflags nouchg "$TOKEN_OS/x" && chmod u+w "$TOKEN_OS/x"'))


def test_chmod_octal_owner_write_runtime_is_denied():
    assert_denied(run_hook("chmod 755 ~/runtimes/token-os/live/bin/x"))


def test_full_bypass_chain_is_denied():
    # The exact reported bypass: nouchg + chmod u+w + edit + relock, one line.
    chain = (
        "chflags nouchg ~/runtimes/Token-OS/live/f && "
        "chmod u+w ~/runtimes/Token-OS/live/f && "
        "echo x >> ~/runtimes/Token-OS/live/f && "
        "chflags uchg ~/runtimes/Token-OS/live/f"
    )
    assert_denied(run_hook(chain))


def test_sanctioned_helper_unlock_runtime_is_denied():
    assert_denied(
        run_hook(
            "cli-tools/scripts/runtime-write-protect.sh unlock "
            "/Users/tokenclaw/runtimes/Token-OS/live"
        )
    )


def test_sanctioned_helper_unlock_without_root_is_denied():
    # With no root args, the helper unlocks its built-in runtime roots.
    assert_denied(run_hook("cli-tools/scripts/runtime-write-protect.sh unlock"))


def test_sanctioned_helper_unlock_runtime_with_force_is_allowed():
    assert_allowed(
        run_hook(
            "cli-tools/scripts/runtime-write-protect.sh unlock --force "
            "/Users/tokenclaw/runtimes/Token-OS/live"
        )
    )


def test_sanctioned_helper_unlock_runtime_with_admin_force_is_allowed():
    assert_allowed(
        run_hook(
            "cli-tools/scripts/runtime-write-protect.sh unlock "
            "--admin-force ~/runtimes/Token-OS/live"
        )
    )


def test_sanctioned_helper_unlock_runtime_with_trailing_force_is_allowed():
    assert_allowed(
        run_hook(
            "cli-tools/scripts/runtime-write-protect.sh unlock "
            "/Users/tokenclaw/runtimes/Token-OS/live --force"
        )
    )


def test_bash_sanctioned_helper_unlock_runtime_is_denied():
    assert_denied(
        run_hook("bash cli-tools/scripts/runtime-write-protect.sh unlock ~/runtimes/Token-OS/live")
    )


def test_cd_runtime_then_relative_helper_unlock_is_denied():
    assert_denied(
        run_hook(
            "cd ~/runtimes/Token-OS/live && cli-tools/scripts/runtime-write-protect.sh unlock ."
        )
    )


def test_cd_runtime_then_relative_chmod_add_write_is_denied():
    assert_denied(run_hook("cd ~/runtimes/Token-OS/live && chmod u+w token-api/main.py"))


# --------------------------------------------------------------------------- #
# ALLOW: re-locking, non-runtime targets, and unrelated commands.
# --------------------------------------------------------------------------- #


def test_relock_chmod_remove_write_is_allowed():
    assert_allowed(run_hook("chmod -R u-w,go-w ~/runtimes/Token-OS/live"))


def test_relock_chflags_set_immutable_is_allowed():
    assert_allowed(run_hook("chflags uchg ~/runtimes/Token-OS/live/x"))


def test_chmod_octal_no_write_runtime_is_allowed():
    # 0444 / 0555 are re-lock modes — no owner write bit.
    assert_allowed(run_hook("chmod 0444 ~/runtimes/Token-OS/live/x"))


def test_chmod_in_worktree_is_unaffected():
    # Worktrees are where agents are SUPPOSED to work — never blocked.
    assert_allowed(run_hook("chmod u+w ~/worktrees/Token-OS/wt-foo/x"))


def test_chmod_non_runtime_is_unaffected():
    assert_allowed(run_hook("chmod u+w ./somefile.py"))


def test_chflags_non_runtime_is_unaffected():
    assert_allowed(run_hook("chflags nouchg ~/Downloads/x"))


def test_command_without_chmod_or_chflags_is_silent():
    # Fast path: no chmod/chflags anywhere -> exits 0 with no parsing.
    assert_allowed(run_hook("ls -la ~/runtimes/Token-OS/live && git status"))


def test_sanctioned_helper_lock_runtime_is_allowed():
    assert_allowed(
        run_hook(
            "cli-tools/scripts/runtime-write-protect.sh lock "
            "/Users/tokenclaw/runtimes/Token-OS/live"
        )
    )


def test_sanctioned_helper_unlock_worktree_is_allowed():
    assert_allowed(
        run_hook(
            "cli-tools/scripts/runtime-write-protect.sh unlock "
            "~/worktrees/Token-OS/wt-runtime-unlock-guard-sanctioned-helper-bypass"
        )
    )


def test_descriptive_dispatch_prompt_is_allowed():
    # Regression: this is a dispatch command whose prompt text merely describes
    # the forbidden operation. The actual command is dispatch, not chmod/chflags
    # or the helper, so it must not be denied by naive substring matching.
    prompt = (
        "Ticket says runtime-write-protect.sh unlock "
        "/Users/tokenclaw/runtimes/Token-OS/live and chmod +w are forbidden."
    )
    assert_allowed(run_hook(f"dispatch --worktree guard-fix --prompt {prompt!r}"))


def test_echo_description_is_allowed():
    assert_allowed(
        run_hook("echo 'do not run chflags nouchg ~/runtimes/Token-OS/live or chmod +w there'")
    )


# --------------------------------------------------------------------------- #
# Escape hatch: legitimate deploy path only.
# --------------------------------------------------------------------------- #


def test_env_escape_hatch_permits_unlock(monkeypatch):
    import os

    env = dict(os.environ)
    env["IMPERIUM_ALLOW_RUNTIME_WRITE"] = "1"
    assert_allowed(run_hook("chflags nouchg ~/runtimes/Token-OS/live/x", env=env))


def test_inline_escape_hatch_permits_unlock():
    assert_allowed(run_hook("IMPERIUM_ALLOW_RUNTIME_WRITE=1 chmod u+w ~/runtimes/Token-OS/live/x"))


def test_inline_escape_hatch_permits_helper_unlock():
    assert_allowed(
        run_hook(
            "IMPERIUM_ALLOW_RUNTIME_WRITE=1 "
            "cli-tools/scripts/runtime-write-protect.sh unlock ~/runtimes/Token-OS/live"
        )
    )
