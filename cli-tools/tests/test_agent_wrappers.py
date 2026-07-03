import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
WRAPPER_ID=wrapper-123
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
    assert payload["wrapper_id"] == "wrapper-123"
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


def _wrapper_env(tmp_path: Path, target: Path, *, fake_curl: bool = True) -> tuple[dict, Path]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    hooks = tmp_path / "hooks.log"
    if fake_curl:
        _write_executable(fakebin / "curl", f'#!/usr/bin/env bash\necho "$*" >> {str(hooks)!r}\n')
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fakebin}:{env.get('PATH', '')}",
            "CLAUDE_BIN": str(target),
            "TOKEN_API_URL": "http://token-api.invalid",
            "TMUXCTLD_URL": "http://tmuxctld.invalid",
            "TOKEN_API_WRAPPER_ID": "wrapper-test-id",
            "TOKEN_WRAPPER_SYNC_SHARED_SKILLS": "0",
        }
    )
    env.pop("TMUX_PANE", None)
    env.pop("TOKEN_API_DISPATCH_RESOLVED_PANE", None)
    return env, hooks


def _read_hooks_after_async(hooks: Path, *, timeout: float = 2.0) -> str:
    deadline = time.time() + timeout
    text = hooks.read_text(encoding="utf-8") if hooks.exists() else ""
    while time.time() < deadline:
        if "/hooks/wrapperend" in text and "/api/hooks/WrapperEnd" in text:
            return text
        time.sleep(0.02)
        text = hooks.read_text(encoding="utf-8") if hooks.exists() else ""
    return text


def _run_wrapper(tmp_path: Path, child_body: str) -> tuple[subprocess.CompletedProcess, str]:
    target = tmp_path / "claude-target"
    _write_executable(target, child_body)
    env, hooks = _wrapper_env(tmp_path, target)
    result = subprocess.run(
        [str(CLI_TOOLS / "scripts" / "agent-wrapper.sh"), "claude", "arg"],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    return result, _read_hooks_after_async(hooks)


def test_wrapper_repairs_shared_skill_roots_before_codex_launch(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical-skills"
    skill = canonical / "sample-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: sample-skill\ndescription: Test skill for Codex wrapper sync.\n---\n\n# Sample\n",
        encoding="utf-8",
    )

    codex_target = tmp_path / "codex-target"
    launched = tmp_path / "codex-launched"
    _write_executable(codex_target, f"#!/usr/bin/env bash\ntouch {str(launched)!r}\nexit 0\n")

    env, _hooks = _wrapper_env(tmp_path, codex_target)
    env["CODEX_BIN"] = str(codex_target)
    env["HOME"] = str(tmp_path / "home")
    env["SKILLS_SYNC_HOME"] = env["HOME"]
    env["SKILLS_SYNC_CANONICAL"] = str(canonical)
    env["TOKEN_WRAPPER_SYNC_SHARED_SKILLS"] = "1"
    env.pop("CLAUDE_BIN", None)

    result = subprocess.run(
        [str(CLI_TOOLS / "scripts" / "agent-wrapper.sh"), "codex"],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert launched.exists()
    assert (tmp_path / "home" / ".codex" / "skills" / "sample-skill").resolve() == skill.resolve()
    assert (tmp_path / "home" / ".agents" / "skills" / "sample-skill").resolve() == skill.resolve()
    assert (tmp_path / "home" / ".claude" / "skills").resolve() == canonical.resolve()
    assert not (tmp_path / "home" / ".claude" / "commands" / "preplan.md").exists()


def test_headless_codex_launch_sets_native_auto_compact_limit(tmp_path: Path) -> None:
    codex_target = tmp_path / "codex-target"
    argv_log = tmp_path / "codex-argv.txt"
    _write_executable(
        codex_target,
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {str(argv_log)!r}\nexit 0\n",
    )

    env, _hooks = _wrapper_env(tmp_path, codex_target)
    env["CODEX_BIN"] = str(codex_target)
    env["CODEX_HEADLESS"] = "1"
    env["HOME"] = str(tmp_path / "home")
    env["TOKEN_WRAPPER_SYNC_SHARED_SKILLS"] = "0"
    env.pop("CLAUDE_BIN", None)

    result = subprocess.run(
        [str(CLI_TOOLS / "scripts" / "agent-wrapper.sh"), "codex", "mission"],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    args = argv_log.read_text(encoding="utf-8").splitlines()
    assert args[0] == "exec"
    pairs = list(zip(args, args[1:], strict=False))
    assert ("-c", "model_auto_compact_token_limit=160000") in pairs


def test_wrapperend_emits_on_normal_child_exit_and_preserves_zero(tmp_path: Path) -> None:
    result, hook_text = _run_wrapper(tmp_path, "#!/usr/bin/env bash\nexit 0\n")

    assert result.returncode == 0
    assert "/hooks/wrapperend" in hook_text
    assert "/api/hooks/WrapperEnd" in hook_text
    assert hook_text.count("/api/hooks/WrapperEnd") == 1


def test_wrapperend_emits_on_nonzero_child_exit_and_preserves_status(tmp_path: Path) -> None:
    result, hook_text = _run_wrapper(tmp_path, "#!/usr/bin/env bash\nexit 7\n")

    assert result.returncode == 7
    assert "/hooks/wrapperend" in hook_text
    assert "/api/hooks/WrapperEnd" in hook_text
    assert "wrapper-test-id" in hook_text


def test_engine_child_inherits_controlling_tty_on_stdin(tmp_path: Path) -> None:
    """Regression: the engine must see a real TTY on stdin.

    A bare ``"$@" &`` makes a non-interactive shell redirect the async child's
    stdin to /dev/null (POSIX), so codex aborts with "stdin is not a terminal"
    and claude silently tolerates it. wrapper_run_child dups the wrapper's own
    stdin (the pane pty) into the child via ``<&0`` to restore the TTY. Driving
    the wrapper under a real pty proves the child inherits it.
    """
    import pty

    target = tmp_path / "claude-target"
    marker = tmp_path / "child-stdin.txt"
    _write_executable(
        target,
        f"#!/usr/bin/env bash\nif [ -t 0 ]; then echo TTY > {str(marker)!r}; "
        f"else echo NOTTY > {str(marker)!r}; fi\nexit 0\n",
    )
    env, _hooks = _wrapper_env(tmp_path, target)

    pid, fd = pty.fork()
    if pid == 0:  # child: become the wrapper, with the pty as its stdin
        os.chdir(tmp_path)
        os.execve(
            str(CLI_TOOLS / "scripts" / "agent-wrapper.sh"),
            [str(CLI_TOOLS / "scripts" / "agent-wrapper.sh"), "claude", "arg"],
            env,
        )
    import select

    deadline = time.time() + 10
    timed_out = True
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        # select keeps the deadline live even if the wrapper goes silent but
        # stays alive — a blocking os.read() would never re-check the timeout.
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            continue
        try:
            if not os.read(fd, 1024):  # EOF: child closed the pty
                timed_out = False
                break
        except OSError:  # pty master raises on child exit
            timed_out = False
            break
    if timed_out:
        # Wrapper wedged (regression): kill so the final reap can't block CI.
        os.kill(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)

    assert not timed_out, "wrapper did not exit within deadline"
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert marker.exists(), "engine child never ran"
    assert marker.read_text(encoding="utf-8").strip() == "TTY"


def _signal_wrapper(tmp_path: Path, sig: signal.Signals, *, spam: int = 1) -> tuple[int, str]:
    ready = tmp_path / "child.ready"
    target = tmp_path / "claude-target"
    _write_executable(
        target,
        f"""#!/usr/bin/env python3
import signal
import sys
import time
from pathlib import Path

Path({str(ready)!r}).touch()

def handler(signum, _frame):
    if signum == signal.SIGINT:
        raise SystemExit(130)
    if signum == signal.SIGTERM:
        raise SystemExit(143)
    if signum == signal.SIGHUP:
        raise SystemExit(129)
    raise SystemExit(128 + signum)

signal.signal(signal.SIGINT, handler)
signal.signal(signal.SIGTERM, handler)
signal.signal(signal.SIGHUP, handler)
while True:
    time.sleep(0.1)
""",
    )
    env, hooks = _wrapper_env(tmp_path, target)
    proc = subprocess.Popen(
        [str(CLI_TOOLS / "scripts" / "agent-wrapper.sh"), "claude", "arg"],
        env=env,
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 5
    while time.time() < deadline and not ready.exists():
        time.sleep(0.02)
    assert ready.exists(), "child never started"
    for _ in range(spam):
        os.kill(proc.pid, sig)
        time.sleep(0.02)
    stdout, stderr = proc.communicate(timeout=5)
    assert stdout == ""
    assert stderr == ""
    return proc.returncode, _read_hooks_after_async(hooks)


def test_wrapperend_emits_on_sigint_and_preserves_signal_status(tmp_path: Path) -> None:
    returncode, hook_text = _signal_wrapper(tmp_path, signal.SIGINT)

    assert returncode == 130
    assert "/hooks/wrapperend" in hook_text
    assert hook_text.count("/api/hooks/WrapperEnd") == 1


def test_wrapperend_emits_on_sigterm_and_preserves_signal_status(tmp_path: Path) -> None:
    returncode, hook_text = _signal_wrapper(tmp_path, signal.SIGTERM)

    assert returncode == 143
    assert "/hooks/wrapperend" in hook_text
    assert hook_text.count("/api/hooks/WrapperEnd") == 1


def test_wrapperend_is_once_under_repeated_interrupt_spam(tmp_path: Path) -> None:
    returncode, hook_text = _signal_wrapper(tmp_path, signal.SIGINT, spam=8)

    assert returncode == 130
    assert hook_text.count("/hooks/wrapperend") == 1
    assert hook_text.count("/api/hooks/WrapperEnd") == 1


class _SlowWrapperHookHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path.endswith("/WrapperStart"):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        time.sleep(5)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args) -> None:
        return


def test_wrapper_cleanup_is_bounded_when_receivers_are_slow(tmp_path: Path) -> None:
    token_api = ThreadingHTTPServer(("127.0.0.1", 0), _SlowWrapperHookHandler)
    tmuxctld = ThreadingHTTPServer(("127.0.0.1", 0), _SlowWrapperHookHandler)
    threads = [
        threading.Thread(target=token_api.serve_forever, daemon=True),
        threading.Thread(target=tmuxctld.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        target = tmp_path / "claude-target"
        _write_executable(target, "#!/usr/bin/env bash\nexit 0\n")
        env, _hooks = _wrapper_env(tmp_path, target, fake_curl=False)
        env["TOKEN_API_URL"] = f"http://127.0.0.1:{token_api.server_address[1]}"
        env["TMUXCTLD_URL"] = f"http://127.0.0.1:{tmuxctld.server_address[1]}"
        env["TOKEN_WRAPPER_TMUXCTLD_MAX_TIME"] = "0.4"
        env["TOKEN_WRAPPER_TOKEN_API_MAX_TIME"] = "0.4"
        started = time.monotonic()
        result = subprocess.run(
            [str(CLI_TOOLS / "scripts" / "agent-wrapper.sh"), "claude", "arg"],
            env=env,
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=3,
        )
        elapsed = time.monotonic() - started
        assert result.returncode == 0
        assert elapsed < 2.0
    finally:
        token_api.shutdown()
        tmuxctld.shutdown()


def test_claude_command_resolves_agent_wrapper_through_symlink(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    called = tmp_path / "claude-called.txt"
    hooks = tmp_path / "hooks.log"
    target = fakebin / "claude-target"
    _write_executable(
        target,
        f'#!/usr/bin/env bash\nprintf \'id=%s args=%s\\n\' "$TOKEN_API_WRAPPER_ID" "$*" > {str(called)!r}\n',
    )
    _write_executable(fakebin / "curl", f'#!/usr/bin/env bash\necho "$*" >> {str(hooks)!r}\n')
    link = tmp_path / "claude"
    link.symlink_to(CLI_TOOLS / "bin" / "claude")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fakebin}:{env.get('PATH', '')}",
            "CLAUDE_BIN": str(target),
            "TOKEN_API_URL": "http://token-api.invalid",
            "TOKEN_API_WRAPPER_ID": "fixed-wrapper-id",
        }
    )
    env.pop("TMUX_PANE", None)
    env.pop("TOKEN_API_DISPATCH_RESOLVED_PANE", None)

    subprocess.run([str(link), "--version"], check=True, env=env, cwd=tmp_path)

    assert called.read_text(encoding="utf-8").strip() == "id=fixed-wrapper-id args=--version"
    hook_text = hooks.read_text(encoding="utf-8")
    assert "/api/hooks/WrapperStart" in hook_text
    assert "/api/hooks/WrapperEnd" in hook_text


def test_codex_command_exports_id_posts_hooks_and_preserves_exit(tmp_path: Path) -> None:
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
        f'#!/usr/bin/env bash\nprintf \'id=%s args=%s\\n\' "$TOKEN_API_WRAPPER_ID" "$*" > {str(codex_called)!r}\nexit 3\n',
    )
    link = tmp_path / "codex"
    link.symlink_to(CLI_TOOLS / "bin" / "codex")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fakebin}:{env.get('PATH', '')}",
            "TOKEN_API_URL": "http://token-api.invalid",
            "TOKEN_API_WRAPPER_ID": "codex-launch-id",
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
    assert codex_called.read_text(encoding="utf-8").strip() == "id=codex-launch-id args=hello world"
    agent_log = log_file.read_text(encoding="utf-8")
    assert "=== Codex Agent 7" in agent_log
    assert "wrapped output" in agent_log
    assert "Exit code: 3" in agent_log
    hook_text = hooks.read_text(encoding="utf-8")
    assert "/api/hooks/WrapperStart" in hook_text
    assert "/api/hooks/WrapperEnd" in hook_text
    assert "codex-launch-id" in hook_text


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
  printf 'main:7\\tmechanicus:worker\\tstack-worker\\n'
elif [[ "$*" == *"%plain"* ]]; then
  printf 'main:1\\t\\t\\n'
elif [[ "$*" == *"%custodes"* ]]; then
  printf 'main:1\\tcouncil:custodes\\t\\n'
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


def test_dispatch_uses_agent_wrapper_not_inline_launcher() -> None:
    dispatch = (CLI_TOOLS / "bin" / "dispatch").read_text(encoding="utf-8")
    assert 'source "${LIB_DIR}/agent-wrapper-common.sh"' in dispatch
    assert "dispatch_codex_launch_inline" not in dispatch
    assert "agent-wrapper.sh" in dispatch
    for engine in ("codex", "claude"):
        assert f"{engine}-wrapper.sh" not in dispatch


def test_wrapperend_posts_token_api_async_before_tmuxctld_bomb() -> None:
    common = (CLI_TOOLS / "lib" / "agent-wrapper-common.sh").read_text(encoding="utf-8")
    end_body = common.split("token_wrapper_end() {", 1)[1].split("}\n\n#", 1)[0]
    assert "token_wrapper_post_hook_async" in end_body
    assert "token_wrapper_post_tmuxctld_wrapperend" in end_body
    assert end_body.index("token_wrapper_post_hook_async") < end_body.index(
        "token_wrapper_post_tmuxctld_wrapperend"
    )
    assert "token_wrapper_enforce_stack_if_needed" not in end_body


def test_wrapperstart_dual_pings_token_api_and_tmuxctld() -> None:
    common = (CLI_TOOLS / "lib" / "agent-wrapper-common.sh").read_text(encoding="utf-8")
    start_body = common.split("token_wrapper_start() {", 1)[1].split("}\n", 1)[0]
    # Both registrars get pinged at wrapper start: token-api (instance row) AND
    # the tmuxctld daemon (wrapper stamp + persona tint) — the daemon leg was the
    # missing half of the empty-stamp/tint dual-ping.
    assert 'token_wrapper_post_hook "WrapperStart"' in start_body
    assert "token_wrapper_post_tmuxctld_wrapperstart" in start_body
    # Local fast-path stamp lands before the daemon re-affirms it.
    assert start_body.index("token_wrapper_stamp_start") < start_body.index(
        "token_wrapper_post_tmuxctld_wrapperstart"
    )
    # The daemon sender targets the /hooks/wrapperstart endpoint.
    assert "/hooks/wrapperstart" in common


def test_stack_enforcement_helper_remains_available() -> None:
    wrapper = (CLI_TOOLS / "scripts" / "agent-wrapper.sh").read_text(encoding="utf-8")
    assert 'token_wrapper_enforce_stack_if_needed "$TMUX_PANE_VALUE"' not in wrapper
    common = (CLI_TOOLS / "lib" / "agent-wrapper-common.sh").read_text(encoding="utf-8")
    assert "token_wrapper_enforce_stack_if_needed()" in common
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
            "CLAUDE_BIN": str(fakebin / "claude-real"),
            "CODEX_BIN": str(fakebin / "codex-real"),
            "TOKEN_API_URL": "http://token-api.invalid",
            "TOKEN_API_WRAPPER_ID": "shim-wrapper-id",
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


def test_install_shims_preserves_vendor_and_installs_bypass_shim(tmp_path: Path) -> None:
    # End-to-end exercise of the installer. A `bash -n` syntax check cannot catch
    # runtime faults like the `local a=$1 b=$a` / set -u unbound-variable trap, so
    # this runs the installer for real against a sandbox via the front-door overrides.
    claude_front = tmp_path / "local-bin" / "claude"
    codex_front = tmp_path / "opt-bin" / "codex"
    claude_front.parent.mkdir(parents=True)
    codex_front.parent.mkdir(parents=True)
    _write_executable(claude_front, '#!/usr/bin/env bash\necho real-claude "$@"\n')
    _write_executable(codex_front, '#!/usr/bin/env bash\necho real-codex "$@"\n')

    env = os.environ.copy()
    env.update(
        {
            "CLAUDE_FRONT_DOOR": str(claude_front),
            "CODEX_FRONT_DOOR": str(codex_front),
        }
    )
    installer = CLI_TOOLS / "bin" / "agent-wrapper-install-shims"
    result = subprocess.run([str(installer)], env=env, text=True, capture_output=True, check=True)
    assert "installed claude shim" in result.stdout
    assert "installed codex shim" in result.stdout

    for front, engine in ((claude_front, "claude"), (codex_front, "codex")):
        real = front.with_name(front.name + ".token-os-real")
        # Vendor binary preserved verbatim, front door replaced with a shim.
        assert real.exists()
        assert real.read_text(encoding="utf-8") == f'#!/usr/bin/env bash\necho real-{engine} "$@"\n'
        shim = front.read_text(encoding="utf-8")
        assert "TOKEN_API_AGENT_WRAPPER_BYPASS" in shim
        assert str(CLI_TOOLS / "bin" / engine) in shim
        assert str(real) in shim

        # The bypass env var routes the shim straight to the preserved vendor binary.
        bypass = os.environ.copy()
        bypass["TOKEN_API_AGENT_WRAPPER_BYPASS"] = "1"
        out = subprocess.run(
            [str(front), "ping"], env=bypass, text=True, capture_output=True, check=True
        )
        assert out.stdout.strip() == f"real-{engine} ping"

    # Re-running is idempotent: it must not clobber the preserved real with a shim.
    again = subprocess.run([str(installer)], env=env, text=True, capture_output=True)
    assert again.returncode == 0
    assert (
        claude_front.with_name("claude.token-os-real").read_text(encoding="utf-8")
        == '#!/usr/bin/env bash\necho real-claude "$@"\n'
    )


def _staple_vault(tmp_path: Path) -> dict[str, str]:
    """Provision a persona DB + vault so the wrapper can build a real staple."""
    import sqlite3

    db = tmp_path / "agents.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE personas (id TEXT PRIMARY KEY, slug TEXT UNIQUE, "
            "display_name TEXT, default_rank TEXT)"
        )
        conn.execute(
            "INSERT INTO personas (id, slug, display_name, default_rank) VALUES (?, ?, ?, ?)",
            ("p0", "blood-angels", "Blood Angels", "astartes"),
        )
    imperium = tmp_path / "vaults" / "Imperium" / "Imperium-ENV"
    (imperium / "Personas" / "Ranks").mkdir(parents=True)
    (tmp_path / "vaults" / "Civic" / "Pax-ENV" / "Personas" / "Ranks").mkdir(parents=True)
    (imperium / "Personas" / "Astartes.md").write_text(
        "## System Prompt\nPERSONA_BODY_MARKER\n", encoding="utf-8"
    )
    (imperium / "Personas" / "Ranks" / "Astartes.md").write_text(
        "RANK_DOCTRINE_MARKER\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env.update(
        {
            "TOKEN_API_DB": str(db),
            "IMPERIUM": str(tmp_path / "vaults" / "Imperium"),
            "CIVIC": str(tmp_path / "vaults" / "Civic"),
        }
    )
    return env


def test_wrapper_composes_staple_rank_first_with_operational_appendix(tmp_path: Path) -> None:
    """A managed worker (TOKEN_API_PERSONA set) gets the rank+persona staple,
    rank doctrine FIRST, persona body second, with the operational appendix folded
    in AFTER — the single injection contract the exec adapters consume."""
    env = _staple_vault(tmp_path)
    env.update(
        {
            "TOKEN_API_PERSONA": "blood-angels",
            "TOKEN_API_VAULT_DOMAIN": "Imperium-ENV",
            "TOKEN_API_SESSION_DOC_ID": "17",
        }
    )
    script = f"""
set -euo pipefail
source {str(CLI_TOOLS / "lib" / "agent-wrapper-common.sh")!r}
TOKEN_WRAPPER_LIB_DIR={str(CLI_TOOLS / "lib")!r}
TMUX_PANE_VALUE=""
token_wrapper_compose_system_text
"""
    result = subprocess.run(
        ["bash", "-c", script], env=env, text=True, capture_output=True, check=True
    )
    out = result.stdout
    rank_i = out.find("RANK_DOCTRINE_MARKER")
    body_i = out.find("PERSONA_BODY_MARKER")
    appendix_i = out.find("Vault domain: Imperium-ENV")
    assert rank_i >= 0 and body_i >= 0 and appendix_i >= 0, out
    assert rank_i < body_i < appendix_i, out
    assert "Instance name prefix:" not in out
    assert "On startup, name this instance" not in out


def test_wrapper_codex_preamble_wraps_staple_in_system_identity(tmp_path: Path) -> None:
    env = _staple_vault(tmp_path)
    env["TOKEN_API_PERSONA"] = "blood-angels"
    script = f"""
set -euo pipefail
source {str(CLI_TOOLS / "lib" / "agent-wrapper-common.sh")!r}
TOKEN_WRAPPER_LIB_DIR={str(CLI_TOOLS / "lib")!r}
TMUX_PANE_VALUE=""
token_wrapper_codex_system_preamble
"""
    result = subprocess.run(
        ["bash", "-c", script], env=env, text=True, capture_output=True, check=True
    )
    out = result.stdout
    assert out.startswith("<SYSTEM IDENTITY>"), out
    assert out.rstrip().endswith("</SYSTEM IDENTITY>"), out
    assert "RANK_DOCTRINE_MARKER" in out
    assert "PERSONA_BODY_MARKER" in out


def test_wrapper_unmanaged_session_gets_no_staple(tmp_path: Path) -> None:
    """No persona env and no singleton pane label → unmanaged → no staple, silent."""
    env = _staple_vault(tmp_path)
    env.pop("TOKEN_API_PERSONA", None)
    script = f"""
set -euo pipefail
source {str(CLI_TOOLS / "lib" / "agent-wrapper-common.sh")!r}
TOKEN_WRAPPER_LIB_DIR={str(CLI_TOOLS / "lib")!r}
TMUX_PANE_VALUE=""
token_wrapper_compose_system_text
"""
    result = subprocess.run(
        ["bash", "-c", script], env=env, text=True, capture_output=True, check=True
    )
    assert result.stdout == ""


def test_static_launch_code_avoids_raw_agent_front_doors() -> None:
    allowed = {
        CLI_TOOLS / "scripts" / "agent-wrapper.sh",
        CLI_TOOLS / "bin" / "claude",
        CLI_TOOLS / "bin" / "codex",
        CLI_TOOLS / "bin" / "agent-wrapper-install-shims",
        # persona-seat.sh:resolve_real_engine() deliberately references the real
        # engine binaries (and their front-door fallbacks) to *bypass* the launch
        # wrapper — re-entering a shim would re-stall the pane reap. It prefers
        # *.token-os-real and greps out wrapper shims, so the front-door paths are
        # last-resort resolution targets, not raw launches. (Introduced by #366.)
        CLI_TOOLS / "scripts" / "persona-seat.sh",
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
