from __future__ import annotations

import json
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
        f"  *'/api/planning/state'*|*'/api/planning/state'*) printf '%s\\n' '{{\"success\":true,\"planning_state\":\"{state}\"}}' ;;\n"
        f"  *'/api/hooks/'*|*'/api/hooks/'*) cat >/dev/null ; printf '%s\\n' '{{\"success\":true}}' ;;\n"
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
            "TOKEN_API_SESSION_ID": "api-instance-1",
        }
    )
    return env, approve_log


def _wait_for_approver(approve_log: pathlib.Path) -> None:
    # ~10s retry budget (was ~1s); widened so CPU contention under parallel runs
    # cannot exhaust the poll before the approver writes.
    for _ in range(200):
        if approve_log.exists() and approve_log.read_text().strip():
            break
        time.sleep(0.05)


def _run_bridge(
    tmp_path: pathlib.Path,
    state: str,
    payload: dict[str, object] | None = None,
    action: str = "Stop",
) -> pathlib.Path:
    env, approve_log = _bridge_env(tmp_path, state)
    subprocess.run(
        ["bash", str(SCRIPT), action],
        input=json.dumps(payload if payload is not None else {"session_id": "codex-1"}).encode(),
        env=env,
        check=True,
    )
    _wait_for_approver(approve_log)
    return approve_log


def _write_transcript(path: pathlib.Path, turns: list[list[dict[str, object]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for i, items in enumerate(turns, start=1):
        lines.extend(json.dumps(item) for item in items)
        lines.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": f"turn-{i}"},
                }
            )
        )
    path.write_text("\n".join(lines) + "\n")


def test_stop_launches_on_planning_state_alone(tmp_path: pathlib.Path) -> None:
    approve_log = _run_bridge(tmp_path, "planning")

    assert (
        approve_log.read_text().strip()
        == "--agent codex --timeout 30 --no-state --pane %12 --instance-id api-instance-1"
    )


def test_stop_launches_on_approving_state_alone(tmp_path: pathlib.Path) -> None:
    approve_log = _run_bridge(tmp_path, "approving")

    assert (
        approve_log.read_text().strip()
        == "--agent codex --timeout 30 --no-state --pane %12 --instance-id api-instance-1"
    )


def test_stop_launches_approver_when_latest_transcript_turn_has_plan_item(
    tmp_path: pathlib.Path,
) -> None:
    transcript = tmp_path / ".codex" / "sessions" / "rollout-codex-1.jsonl"
    _write_transcript(
        transcript,
        [
            [
                {
                    "type": "response_item",
                    "payload": {"item": {"type": "Plan", "text": "do it"}},
                }
            ]
        ],
    )

    approve_log = _run_bridge(tmp_path, "none", {"session_id": "codex-1"})

    assert (
        approve_log.read_text().strip()
        == "--agent codex --timeout 30 --no-state --pane %12 --instance-id api-instance-1"
    )


def test_stop_launches_approver_when_latest_transcript_turn_has_proposed_plan(
    tmp_path: pathlib.Path,
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(
        transcript,
        [
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "<proposed_plan>\nPlan\n</proposed_plan>",
                            }
                        ],
                    },
                }
            ]
        ],
    )

    approve_log = _run_bridge(
        tmp_path,
        "none",
        {"session_id": "codex-1", "transcript_path": str(transcript)},
    )

    assert (
        approve_log.read_text().strip()
        == "--agent codex --timeout 30 --no-state --pane %12 --instance-id api-instance-1"
    )


def test_stop_does_not_launch_when_only_older_transcript_turn_has_plan(
    tmp_path: pathlib.Path,
) -> None:
    transcript = tmp_path / ".codex" / "sessions" / "rollout-codex-1.jsonl"
    _write_transcript(
        transcript,
        [
            [
                {
                    "type": "response_item",
                    "payload": {"item": {"type": "Plan", "text": "old"}},
                }
            ],
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                }
            ],
        ],
    )

    approve_log = _run_bridge(tmp_path, "none", {"session_id": "codex-1"})

    assert not approve_log.exists()


def test_stop_does_not_launch_approver_when_not_planning(tmp_path: pathlib.Path) -> None:
    approve_log = _run_bridge(tmp_path, "none")

    assert not approve_log.exists()


def test_stop_launches_approver_when_payload_has_proposed_plan(tmp_path: pathlib.Path) -> None:
    approve_log = _run_bridge(
        tmp_path,
        "none",
        {
            "session_id": "codex-1",
            "message": "<proposed_plan>\nPlan\n</proposed_plan>",
        },
    )

    assert (
        approve_log.read_text().strip()
        == "--agent codex --timeout 30 --no-state --pane %12 --instance-id api-instance-1"
    )


def test_post_tool_use_in_open_plan_turn_launches_longer_watcher(tmp_path: pathlib.Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "collaboration_mode_kind": "plan"},
            }
        )
        + "\n"
        + json.dumps({"type": "response_item", "payload": {"type": "function_call"}})
        + "\n"
    )

    approve_log = _run_bridge(
        tmp_path,
        "none",
        {"session_id": "codex-1", "transcript_path": str(transcript)},
        action="PostToolUse",
    )

    assert (
        approve_log.read_text().strip()
        == "--agent codex --timeout 120 --no-state --pane %12 --instance-id api-instance-1"
    )


def test_user_prompt_submit_plan_command_launches_longer_watcher(tmp_path: pathlib.Path) -> None:
    approve_log = _run_bridge(
        tmp_path,
        "none",
        {"session_id": "codex-1", "prompt": "/plan make a tiny plan"},
        action="UserPromptSubmit",
    )

    assert (
        approve_log.read_text().strip()
        == "--agent codex --timeout 300 --no-state --pane %12 --instance-id api-instance-1"
    )
