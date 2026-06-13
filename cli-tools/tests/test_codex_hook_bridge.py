from __future__ import annotations

import os
import pathlib
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex-hook-bridge.sh"


def _bridge_env(tmp_path: pathlib.Path, state: str) -> tuple[dict[str, str], pathlib.Path]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    approve_log = tmp_path / "approver.log"
    curl_log = tmp_path / "curl.log"
    approver = tmp_path / "approver"

    (fakebin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {curl_log!s}\n"
        'case "$*" in\n'
        f"  *'/api/planning/state'*) printf '%s\\n' '{{\"success\":true,\"planning_state\":\"{state}\"}}' ;;\n"
        "esac\n"
    )
    approver.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {approve_log!s}\n")
    for script in fakebin.iterdir():
        script.chmod(0o755)
    approver.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fakebin}:{env['PATH']}",
            "HOME": str(tmp_path),
            "TOKEN_API_URL": "http://token-api.test",
            "TMUX_PANE": "%12",
            "TOKEN_API_PLAN_APPROVER": str(approver),
            "TOKEN_API_DISABLE_SESSION_RESUME": "1",
        }
    )
    return env, approve_log


def _run_bridge(tmp_path: pathlib.Path, state: str) -> pathlib.Path:
    env, approve_log = _bridge_env(tmp_path, state)
    subprocess.run(
        ["bash", str(SCRIPT), "Stop"],
        input=b'{"session_id":"codex-1"}',
        env=env,
        check=True,
    )
    for _ in range(20):
        if approve_log.exists():
            break
        time.sleep(0.05)
    return approve_log


def test_stop_launches_clear_context_approver_when_planning(tmp_path):
    approve_log = _run_bridge(tmp_path, "planning")

    assert approve_log.read_text().strip() == "--pane %12 --agent codex --timeout 10"


def test_stop_launches_clear_context_approver_when_approving(tmp_path):
    approve_log = _run_bridge(tmp_path, "approving")

    assert approve_log.read_text().strip() == "--pane %12 --agent codex --timeout 10"


def test_stop_does_not_launch_approver_when_not_planning(tmp_path):
    approve_log = _run_bridge(tmp_path, "none")

    assert not approve_log.exists()
