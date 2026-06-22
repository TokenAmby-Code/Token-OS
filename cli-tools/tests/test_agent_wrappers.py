import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_TOOLS = REPO_ROOT / "cli-tools"


def test_wrapper_scripts_are_bash_syntax_valid() -> None:
    subprocess.run(
        [
            "bash",
            "-n",
            str(CLI_TOOLS / "lib" / "agent-wrapper-common.sh"),
            str(CLI_TOOLS / "scripts" / "agent-wrapper.sh"),
            str(CLI_TOOLS / "scripts" / "claude-wrapper.sh"),
            str(CLI_TOOLS / "scripts" / "codex-wrapper.sh"),
            str(CLI_TOOLS / "bin" / "claude"),
            str(CLI_TOOLS / "bin" / "codex"),
            str(CLI_TOOLS / "bin" / "agent-wrapper-install-shims"),
        ],
        check=True,
    )


def test_common_payload_includes_action_and_resolved_pane() -> None:
    script = f"""
set -euo pipefail
source {str(CLI_TOOLS / "lib" / "agent-wrapper-common.sh")!r}
API_URL=http://example.invalid
LAUNCHER=unit-launcher
ENGINE=codex
WRAPPER_LAUNCH_ID=wrapper-123
WORKING_DIR=/tmp/work
TMUX_PANE_VALUE=%42
TOKEN_API_DISPATCH_RESOLVED_PANE=%42
TOKEN_API_CODEX_PROFILE=test-profile
payload=$(token_wrapper_build_payload WrapperEnd 7)
printf '%s' "$payload"
"""
    result = subprocess.run(["bash", "-c", script], check=True, text=True, capture_output=True)
    payload = json.loads(result.stdout)
    assert payload["action"] == "WrapperEnd"
    assert payload["wrapper_launch_id"] == "wrapper-123"
    assert payload["engine"] == "codex"
    assert payload["tmux_pane"] == "%42"
    assert payload["exit_code"] == 7
    assert payload["env"]["TOKEN_API_DISPATCH_RESOLVED_PANE"] == "%42"
    assert payload["env"]["TOKEN_API_CODEX_PROFILE"] == "test-profile"


def test_common_cleanup_prefers_tmuxctl_clear_runtime() -> None:
    tmuxctl = CLI_TOOLS / "bin" / "tmuxctl"
    # The helper resolves ../bin/tmuxctl relative to the real checked-out lib.
    # Assert textually instead of replacing that file in-place.
    common = (CLI_TOOLS / "lib" / "agent-wrapper-common.sh").read_text(encoding="utf-8")
    assert "clear-runtime --pane" in common
    assert "tmux_runtime_cleanup_pane" in common
    assert tmuxctl.exists()


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_claude_wrapper_resolves_common_lib_through_symlink(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    called = tmp_path / "claude-called.txt"
    hooks = tmp_path / "hooks.log"
    target = fakebin / "claude-target"
    _write_executable(
        target,
        f'#!/usr/bin/env bash\nprintf \'id=%s args=%s\\n\' "$TOKEN_API_WRAPPER_LAUNCH_ID" "$*" > {str(called)!r}\n',
    )
    _write_executable(fakebin / "curl", f'#!/usr/bin/env bash\necho "$*" >> {str(hooks)!r}\n')
    link = tmp_path / "claude-wrapper.sh"
    link.symlink_to(CLI_TOOLS / "scripts" / "claude-wrapper.sh")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fakebin}:{env.get('PATH', '')}",
            "CLAUDE_WRAPPER_TARGET": str(target),
            "TOKEN_API_URL": "http://token-api.invalid",
            "TOKEN_API_WRAPPER_LAUNCH_ID": "fixed-wrapper-id",
        }
    )
    env.pop("TMUX_PANE", None)
    env.pop("TOKEN_API_DISPATCH_RESOLVED_PANE", None)

    subprocess.run([str(link), "--version"], check=True, env=env, cwd=tmp_path)

    assert called.read_text(encoding="utf-8").strip() == "id=fixed-wrapper-id args=--version"
    hook_text = hooks.read_text(encoding="utf-8")
    assert "/api/hooks/WrapperStart" in hook_text
    assert "/api/hooks/WrapperEnd" in hook_text


def test_codex_wrapper_exports_id_posts_hooks_and_preserves_exit(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    hooks = tmp_path / "hooks.log"
    codex_called = tmp_path / "codex-called.txt"
    log_file = tmp_path / "agent.log"
    _write_executable(fakebin / "curl", f'#!/usr/bin/env bash\necho "$*" >> {str(hooks)!r}\n')
    _write_executable(
        fakebin / "script",
        """#!/usr/bin/env bash
set -euo pipefail
cmd=""
log=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c) cmd="$2"; shift 2 ;;
    -a|-f|-e) shift ;;
    *) log="$1"; shift ;;
  esac
done
printf '\033[31mwrapped output\033[0m\n' >> "$log"
bash -c "$cmd"
""",
    )
    codex_target = fakebin / "codex-target"
    _write_executable(
        codex_target,
        f'#!/usr/bin/env bash\nprintf \'id=%s args=%s\\n\' "$TOKEN_API_WRAPPER_LAUNCH_ID" "$*" > {str(codex_called)!r}\nexit 3\n',
    )
    link = tmp_path / "codex-wrapper.sh"
    link.symlink_to(CLI_TOOLS / "scripts" / "codex-wrapper.sh")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fakebin}:{env.get('PATH', '')}",
            "TOKEN_API_URL": "http://token-api.invalid",
            "TOKEN_API_WRAPPER_LAUNCH_ID": "codex-wrapper-id",
            "TOKEN_API_DISPATCH_RESOLVED_PANE": "%99",
        }
    )
    env.pop("TMUX_PANE", None)

    result = subprocess.run(
        [str(link), "7", str(log_file), str(codex_target), "hello world"],
        env=env,
        cwd=tmp_path,
    )

    assert result.returncode == 3
    assert (
        codex_called.read_text(encoding="utf-8").strip() == "id=codex-wrapper-id args=hello world"
    )
    agent_log = log_file.read_text(encoding="utf-8")
    assert "=== Codex Agent 7" in agent_log
    assert "wrapped output" in agent_log
    assert "Exit code: 3" in agent_log
    hook_text = hooks.read_text(encoding="utf-8")
    assert "/api/hooks/WrapperStart" in hook_text
    assert "/api/hooks/WrapperEnd" in hook_text
    assert "codex-wrapper-id" in hook_text


def test_wrapper_stack_enforcement_runs_only_for_stack_panes(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (tmp_path / "lib").mkdir()
    calls = tmp_path / "tmuxctl.calls"
    tmux = fakebin / "tmux"
    _write_executable(
        tmux,
        """#!/usr/bin/env bash
if [[ "$*" == *"%stack"* ]]; then
  printf 'main:7\\tlegion:worker\\tstack-worker\\n'
elif [[ "$*" == *"%plain"* ]]; then
  printf 'main:1\\t\\t\\n'
elif [[ "$*" == *"%custodes"* ]]; then
  printf 'main:1\\tlegion:custodes\\t\\n'
fi
""",
    )
    _write_executable(
        fakebin / "tmuxctl",
        f"""#!/usr/bin/env bash
echo "$*" >> {str(calls)!r}
""",
    )
    script = f"""
set -euo pipefail
PATH={str(fakebin)!r}:$PATH
source {str(CLI_TOOLS / "lib" / "agent-wrapper-common.sh")!r}
TOKEN_WRAPPER_LIB_DIR={str(tmp_path / "lib")!r}
TMUX_PANE_VALUE=%plain
token_wrapper_enforce_stack_if_needed %plain
TMUX_PANE_VALUE=%custodes
token_wrapper_enforce_stack_if_needed %custodes
TMUX_PANE_VALUE=%stack
token_wrapper_enforce_stack_if_needed %stack
wait
"""
    subprocess.run(["bash", "-c", script], check=True, text=True, capture_output=True)
    assert (
        calls.read_text(encoding="utf-8").strip()
        == "stack enforce --window main:7 --kill-pending-clear"
    )


def test_dispatch_uses_codex_wrapper_not_inline_launcher() -> None:
    dispatch = (CLI_TOOLS / "bin" / "dispatch").read_text(encoding="utf-8")
    assert 'source "${LIB_DIR}/agent-wrapper-common.sh"' in dispatch
    assert "dispatch_codex_launch_inline" not in dispatch
    assert "codex-wrapper.sh" in dispatch
    assert "claude-wrapper.sh" in dispatch


def test_agent_wrapper_owns_stack_enforcement() -> None:
    wrapper = (CLI_TOOLS / "scripts" / "agent-wrapper.sh").read_text(encoding="utf-8")
    assert 'token_wrapper_enforce_stack_if_needed "$TMUX_PANE_VALUE"' not in wrapper
    common = (CLI_TOOLS / "lib" / "agent-wrapper-common.sh").read_text(encoding="utf-8")
    assert 'token_wrapper_enforce_stack_if_needed "$TMUX_PANE_VALUE"' in common
    assert "tmuxctl stack enforce" in common


def test_command_shims_route_through_wrappers_and_bypass_to_real(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    hooks = tmp_path / "hooks.log"
    claude_called = tmp_path / "claude-called.txt"
    codex_called = tmp_path / "codex-called.txt"
    _write_executable(fakebin / "curl", f'#!/usr/bin/env bash\necho "$*" >> {str(hooks)!r}\n')
    _write_executable(
        fakebin / "claude-real",
        f'#!/usr/bin/env bash\nprintf \'claude bypass=%s args=%s\\n\' "${{TOKEN_API_AGENT_WRAPPER_BYPASS:-}}" "$*" >> {str(claude_called)!r}\n',
    )
    _write_executable(
        fakebin / "codex-real",
        f'#!/usr/bin/env bash\nprintf \'codex bypass=%s args=%s\\n\' "${{TOKEN_API_AGENT_WRAPPER_BYPASS:-}}" "$*" >> {str(codex_called)!r}\n',
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{CLI_TOOLS / 'bin'}:{fakebin}:{env.get('PATH', '')}",
            "CLAUDE_WRAPPER_TARGET": str(fakebin / "claude-real"),
            "CODEX_WRAPPER_TARGET": str(fakebin / "codex-real"),
            "TOKEN_API_URL": "http://token-api.invalid",
            "TOKEN_API_WRAPPER_LAUNCH_ID": "shim-wrapper-id",
        }
    )
    env.pop("TMUX_PANE", None)
    env.pop("TOKEN_API_DISPATCH_RESOLVED_PANE", None)

    script = """
set -euo pipefail
command -v claude
command -v codex
command claude alpha
command codex beta
TOKEN_API_AGENT_WRAPPER_BYPASS=1 command claude raw-alpha
TOKEN_API_AGENT_WRAPPER_BYPASS=1 command codex raw-beta
"""
    result = subprocess.run(
        ["bash", "-c", script], env=env, cwd=tmp_path, text=True, capture_output=True, check=True
    )
    lines = result.stdout.splitlines()
    assert lines[0] == str(CLI_TOOLS / "bin" / "claude")
    assert lines[1] == str(CLI_TOOLS / "bin" / "codex")
    assert claude_called.read_text(encoding="utf-8").splitlines() == [
        "claude bypass=1 args=alpha",
        "claude bypass= args=raw-alpha",
    ]
    assert codex_called.read_text(encoding="utf-8").splitlines() == [
        "codex bypass=1 args=beta",
        "codex bypass= args=raw-beta",
    ]


def test_static_launch_code_avoids_raw_agent_front_doors() -> None:
    allowed = {
        CLI_TOOLS / "scripts" / "agent-wrapper.sh",
        CLI_TOOLS / "bin" / "claude",
        CLI_TOOLS / "bin" / "codex",
        CLI_TOOLS / "bin" / "agent-wrapper-install-shims",
    }
    launch_files = [
        CLI_TOOLS / "bin" / "dispatch",
        CLI_TOOLS / "bin" / "work-loop",
        CLI_TOOLS / "lib" / "shell-aliases.sh",
        *sorted((CLI_TOOLS / "scripts").glob("*.sh")),
    ]
    forbidden = [
        "$HOME/.local/bin/claude",
        "~/.local/bin/claude",
        "/Users/tokenclaw/.local/bin/claude",
        "/opt/homebrew/bin/codex",
        "command -v codex",
    ]
    offenders: list[str] = []
    for path in launch_files:
        if path in allowed or not path.exists():
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        for needle in forbidden:
            if needle in body:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")
    assert offenders == []
