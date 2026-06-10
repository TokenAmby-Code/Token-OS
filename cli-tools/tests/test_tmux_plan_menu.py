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


def test_preplan_codex_inserts_dollar_leader() -> None:
    out = _dry("preplan", "codex")
    assert "agent=codex" in out
    assert "insert:$preplan" in out
    assert "subscribe:preplan_plan" in out


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
    env.update(env_overrides or {})
    subprocess.check_output(
        [str(SCRIPT), "--pane", "%1", "--agent", "claude", "--selection", "shift+tab"],
        text=True,
        timeout=10,
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
