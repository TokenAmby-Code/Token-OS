from __future__ import annotations

import os
import pathlib
import shlex
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "tmux-plan-menu"


def _dry(
    selection: str,
    agent: str | None = None,
    *,
    pane: str = "%1",
    env: dict[str, str] | None = None,
) -> str:
    cmd = [str(SCRIPT), "--pane", pane, "--dry-run", "--selection", selection]
    if agent:
        cmd += ["--agent", agent]
    return subprocess.check_output(cmd, text=True, timeout=10, env=env).strip()


def test_preplan_claude_inserts_slash_leader_and_subscribes() -> None:
    out = _dry("preplan", "claude")
    assert "selection=preplan" in out
    assert "agent=claude" in out
    assert "insert:/preplan" in out
    assert "subscribe:preplan_plan" in out
    assert "state:preplanning" in out


def test_preplan_codex_inserts_dollar_leader_and_tabs_to_sink_skill() -> None:
    out = _dry("preplan", "codex")
    assert "agent=codex" in out
    assert "insert:$preplan" in out
    assert "subscribe:preplan_plan" in out
    assert "sink:Tab" in out


def test_plan_inserts_universal_slash_plan() -> None:
    out = _dry("plan")
    assert "selection=plan" in out
    assert "insert:/plan" in out
    assert "state:planning" in out


def test_compact_inserts_universal_slash_compact() -> None:
    out = _dry("compact")
    assert "selection=compact" in out
    assert "insert:/compact" in out


def test_shift_tab_forwards_literal_btab() -> None:
    out = _dry("shift+tab")
    assert "selection=shift+tab" in out
    assert "send-keys:BTab" in out


def test_cancel_is_noop() -> None:
    out = _dry("cancel")
    assert "selection=cancel" in out
    assert "noop" in out


def test_literal_pane_arg_falls_back_to_btab_pane_env() -> None:
    # display-popup passes the binding's #{pane_id} literally (it does not expand
    # the shell-command), so a non-%id --pane must be ignored and the BTAB_PANE
    # env (expanded at key dispatch) used to lock the invoking pane instead.
    env = {**os.environ, "BTAB_PANE": "%7"}
    out = _dry("plan", pane="#{pane_id}", env=env)
    assert "pane=%7" in out


def _run_action_capturing_send_env(
    tmp_path: pathlib.Path, *, env_overrides: dict[str, str] | None = None
) -> tuple[str, str]:
    """Run a real (non-dry) action and capture the gate disposition its send sees.

    Stub ``tmux`` on PATH so the ``shift+tab`` forward (`tmux send-keys ... BTab`)
    records the inherited gate env instead of touching a live pane. ``--agent
    claude`` short-circuits the background harness probe, leaving that one send as
    the only ``tmux`` call.
    """
    recorder = tmp_path / "send_env.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n%s\\n" "$TMUX_SEND_GATE_POLICY" "$TMUX_SEND_GATE_ALLOW" > {shlex.quote(str(recorder))}\n'
        "exit 0\n"
    )
    fake_tmux.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    # The decomposed cursor ops now route through tmuxctl -> the send gate, so run
    # the action synchronously (no detach) against the stub tmux, isolate the gate
    # DB to a throwaway path (never the live agents.db), shrink the gated-send
    # volume, and bound any quiet-hours delay so the test can never block.
    env.update(
        {
            "TMUX_PLAN_MENU_NO_DETACH": "1",
            "IMPERIUM_TMUX_BIN": str(fake_tmux),
            "TOKEN_API_DB": str(tmp_path / "gate.db"),
            "TMUX_PLAN_MENU_PAGE_UPS": "2",
            "TMUX_PLAN_MENU_PAGE_DOWNS": "2",
            "TMUX_SEND_GATE_DELAY_TIMEOUT": "0.1",
        }
    )
    env.update(env_overrides or {})
    subprocess.check_output(
        [str(SCRIPT), "--pane", "%1", "--agent", "claude", "--selection", "shift+tab"],
        text=True,
        timeout=20,
        env=env,
    )
    policy, allow = recorder.read_text().splitlines()[:2]
    return policy, allow


def test_actions_pierce_typing_guard_by_default(tmp_path: pathlib.Path) -> None:
    # The menu is opened by a keystroke, so the recent-typing gate is always
    # freshly active when an action fires. Its sends are direct Emperor input and
    # must pierce (not delay behind the very keystroke that opened the menu) — the
    # sanctioned, audited disposition tmux-dictate uses for the same reason.
    policy, allow = _run_action_capturing_send_env(tmp_path)
    assert policy == "pierce"
    assert allow == "tmux-plan-menu-direct-input"


def test_outer_gate_disposition_is_respected(tmp_path: pathlib.Path) -> None:
    # `${VAR:-default}` defers to an explicit outer disposition (e.g. a test
    # harness or a caller that deliberately wants the send delayed/cancelled).
    policy, allow = _run_action_capturing_send_env(
        tmp_path,
        env_overrides={
            "TMUX_SEND_GATE_POLICY": "delay",
            "TMUX_SEND_GATE_ALLOW": "outer-reason",
        },
    )
    assert policy == "delay"
    assert allow == "outer-reason"


def _run_action_recording_sends(
    tmp_path: pathlib.Path,
    selection: str,
    *,
    agent: str | None = "claude",
    api_url: str | None = None,
) -> tuple[list[str], pathlib.Path]:
    """Run a real (non-dry) action and return (recorded tmux sends, failure-log path).

    Stub ``tmux`` to APPEND each invocation's argv, force tmuxctl onto the stub,
    isolate the gate DB and ``$HOME`` (so the failure log lands in tmp), run the
    action synchronously, and shrink the gated-send volume.
    """
    recorder = tmp_path / "sends.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {shlex.quote(str(recorder))}\nexit 0\n'
    )
    fake_tmux.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(home),
        "IMPERIUM_TMUX_BIN": str(fake_tmux),
        "TOKEN_API_DB": str(tmp_path / "gate.db"),
        "TMUX_PLAN_MENU_NO_DETACH": "1",
        "TMUX_PLAN_MENU_PAGE_UPS": "3",
        "TMUX_PLAN_MENU_PAGE_DOWNS": "3",
        "TMUX_SEND_GATE_DELAY_TIMEOUT": "0.1",
    }
    cmd = [str(SCRIPT), "--pane", "%1", "--selection", selection]
    if agent:
        cmd += ["--agent", agent]
    if api_url is not None:
        cmd += ["--api-url", api_url]
    subprocess.check_output(cmd, text=True, timeout=20, env=env)
    lines = recorder.read_text().splitlines() if recorder.exists() else []
    logfile = home / ".claude" / "logs" / "tmux-plan-menu.log"
    return lines, logfile


def test_cancel_does_not_touch_pane_without_prebuffer(tmp_path: pathlib.Path) -> None:
    # Prebuffer is off by default, so cancel should not spam PgDn/End into the
    # live composer.
    lines, logfile = _run_action_recording_sends(tmp_path, "cancel")
    assert lines == []
    assert not logfile.exists()


def test_shift_tab_forwards_btab_without_prompt_restore_when_no_prebuffer(tmp_path: pathlib.Path) -> None:
    lines, logfile = _run_action_recording_sends(tmp_path, "shift+tab")
    assert "send-keys -t %1 BTab" in lines
    assert not any("PgDn" in line for line in lines)
    assert not any(" -l " in line for line in lines)  # shift+tab inserts no leader
    assert not logfile.exists()


def test_preplan_skips_insert_and_logs_on_subscribe_failure(tmp_path: pathlib.Path) -> None:
    # The one-shot Stop subscription must arm BEFORE the leader is inserted. With
    # an unreachable API the subscribe fails, so the leader insert is skipped (no
    # /plan would ever come) and the failure is logged. The cursor still restores.
    lines, logfile = _run_action_recording_sends(
        tmp_path, "preplan", agent="claude", api_url="http://127.0.0.1:1"
    )
    assert not any(" -l " in line for line in lines)  # no leader inserted
    assert not any("PgDn" in line for line in lines)  # no cursor move happened
    assert logfile.exists()  # post-mortem log written
    content = logfile.read_text()
    assert "selection=preplan" in content
    assert "step=subscribe" in content




def test_codex_preplan_tabs_after_dollar_skill_on_successful_subscribe(tmp_path: pathlib.Path) -> None:
    recorder = tmp_path / "sends.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {shlex.quote(str(recorder))}\nexit 0\n'
    )
    fake_tmux.chmod(0o755)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *'/api/hooks/subscribe'*) printf '%s\\n' '{\"success\":true}' ;;\n"
        "  *) printf '%s\\n' '{\"success\":true}' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    fake_curl.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(home),
        "IMPERIUM_TMUX_BIN": str(fake_tmux),
        "TOKEN_API_DB": str(tmp_path / "gate.db"),
        "TMUX_PLAN_MENU_NO_DETACH": "1",
        "TMUX_PLAN_MENU_PAGE_UPS": "3",
        "TMUX_PLAN_MENU_PAGE_DOWNS": "3",
        "TMUX_SEND_GATE_DELAY_TIMEOUT": "0.1",
    }
    subprocess.check_output(
        [str(SCRIPT), "--pane", "%1", "--selection", "preplan", "--agent", "codex", "--api-url", "http://stub"],
        text=True,
        timeout=20,
        env=env,
    )
    lines = recorder.read_text().splitlines()
    assert "send-keys -l $preplan" not in lines  # target must always be present
    assert "send-keys -t %1 -l $preplan" in lines
    assert "send-keys -t %1 Tab" in lines
    assert lines.index("send-keys -t %1 -l $preplan") < lines.index("send-keys -t %1 Tab")


def _run_action_with_stubs(
    tmp_path: pathlib.Path,
    selection: str,
    *,
    agent: str | None = "claude",
    api_url: str | None = None,
    tmux_body: str | None = None,
    curl_body: str | None = None,
) -> pathlib.Path:
    """Run a real (non-dry) action against custom ``tmux``/``curl`` stubs.

    Returns the failure-log path (``$HOME/.claude/logs/tmux-plan-menu.log``). The
    stubs let a test force a cursor-op failure (tmux exits non-zero with stderr) or
    observe the subscribe retry (curl records each attempt).
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(tmux_body or "#!/usr/bin/env bash\nexit 0\n")
    fake_tmux.chmod(0o755)
    if curl_body is not None:
        fake_curl = fake_bin / "curl"
        fake_curl.write_text(curl_body)
        fake_curl.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(home),
        "IMPERIUM_TMUX_BIN": str(fake_tmux),
        "TOKEN_API_DB": str(tmp_path / "gate.db"),
        "TMUX_PLAN_MENU_NO_DETACH": "1",
        "TMUX_PLAN_MENU_PAGE_UPS": "3",
        "TMUX_PLAN_MENU_PAGE_DOWNS": "3",
        "TMUX_SEND_GATE_DELAY_TIMEOUT": "0.1",
    }
    cmd = [str(SCRIPT), "--pane", "%1", "--selection", selection]
    if agent:
        cmd += ["--agent", agent]
    if api_url is not None:
        cmd += ["--api-url", api_url]
    subprocess.check_output(cmd, text=True, timeout=20, env=env)
    return home / ".claude" / "logs" / "tmux-plan-menu.log"


def test_failed_cursor_op_logs_real_tmux_stderr(tmp_path: pathlib.Path) -> None:
    # A residual cursor-op failure must carry the REAL tmux error into the log, not
    # just an rc — the diagnostic safety net per the batch+capture path. Fail only
    # the PgDn prompt-end send (PgUp prompt-start still succeeds) with a recognizable
    # stderr string and assert it is captured alongside step=prompt-end.
    tmux_body = (
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *PgDn*) echo "cant find pane: %1" >&2; exit 1 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    logfile = _run_action_with_stubs(tmp_path, "plan", tmux_body=tmux_body)
    assert logfile.exists()
    content = logfile.read_text()
    assert "step=prompt-end" in content
    assert "cant find pane" in content  # the real tmux stderr, not just rc=1


def test_failed_prompt_start_logs_real_tmux_stderr(tmp_path: pathlib.Path) -> None:
    # The prompt-start failures (prebuffer + the synchronous _ensure fallback) were
    # previously swallowed by `2>&1`. Fail only the PgUp prompt-start send and assert
    # the real tmux stderr now reaches the log under step=prompt-start.
    tmux_body = (
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *PgUp*) echo "prompt-start boom" >&2; exit 1 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    logfile = _run_action_with_stubs(tmp_path, "plan", agent="claude", tmux_body=tmux_body)
    assert logfile.exists()
    content = logfile.read_text()
    assert "step=prompt-start" in content
    assert "prompt-start boom" in content  # the real tmux stderr, not just a silent miss


def test_subscribe_preplan_retries_once_then_fails_closed(tmp_path: pathlib.Path) -> None:
    # A timed-out subscribe (curl exit 28) is retried exactly once before failing
    # closed. The retry must run (two curl attempts) and, with both timing out, the
    # leader insert is still skipped and the failure logged — never an unarmed leader.
    attempts = tmp_path / "curl_attempts.txt"
    curl_body = (
        "#!/usr/bin/env bash\n"
        f"echo attempt >> {shlex.quote(str(attempts))}\n"
        "exit 28\n"  # simulate --max-time timeout
    )
    logfile = _run_action_with_stubs(
        tmp_path, "preplan", agent="claude", api_url="http://stub.invalid/", curl_body=curl_body
    )
    assert attempts.exists()
    assert len(attempts.read_text().splitlines()) == 2  # one retry after the first timeout
    assert logfile.exists()
    content = logfile.read_text()
    assert "step=subscribe" in content
