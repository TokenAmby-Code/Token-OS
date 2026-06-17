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
            str(CLI_TOOLS / "scripts" / "claude-wrapper.sh"),
            str(CLI_TOOLS / "scripts" / "codex-wrapper.sh"),
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
