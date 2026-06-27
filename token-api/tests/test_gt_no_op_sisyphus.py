import json
import sqlite3
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from golden_throne_noop import worktree_fingerprint


def _insert_gt(
    db_path: Path,
    *,
    counter: int = 0,
    hook_driven: int = 1,
    fingerprint: str | None = None,
    working_dir: str = "/tmp",
) -> str:
    iid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO golden_throne (id, zealotry) VALUES (1, 4)")
    conn.execute(
        """INSERT INTO instances
           (id, name, working_dir, origin_type, device_id, status, golden_throne,
            zealotry, created_at, last_activity, hook_driven, gt_no_op_counter,
            gt_no_op_summaries_json, gt_last_dispatch_fingerprint)
           VALUES (?, ?, ?, 'local', 'Mac-Mini', 'working', '1', 4, ?, ?, ?, ?, ?, ?)""",
        (
            iid,
            f"gt-{iid[:8]}",
            working_dir,
            now,
            now,
            hook_driven,
            counter,
            json.dumps([f"prior noop {i + 1}" for i in range(counter)]),
            fingerprint,
        ),
    )
    conn.commit()
    conn.close()
    return iid


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "file.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    return repo


def _assistant_tail(text: str) -> str:
    return json.dumps({"message": {"role": "assistant", "content": text}})


def _assistant_tool_tail(name: str = "Bash") -> str:
    return json.dumps(
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will inspect it."},
                    {"type": "tool_use", "name": name, "input": {}},
                ],
            }
        }
    )


def _row(db_path: Path, iid: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM instances WHERE id = ?", (iid,)).fetchone()
    conn.close()
    return row


def _events(db_path: Path, iid: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT event_type, details FROM events WHERE instance_id = ? ORDER BY id", (iid,)
    ).fetchall()
    conn.close()
    return rows


def test_one_gt_no_op_increments_counter_and_stays_gt(app_env, monkeypatch):
    main = app_env.main
    monkeypatch.setattr(main.shared, "get_quiet_hours_status", lambda: {"active": False})
    client = TestClient(main.app)
    iid = _insert_gt(app_env.db_path)

    resp = client.post(
        "/api/hooks/Stop",
        json={
            "session_id": iid,
            "transcript_tail": _assistant_tail("No changes needed. ScheduleWakeup armed."),
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    row = _row(app_env.db_path, iid)
    assert row["gt_no_op_counter"] == 1
    assert row["golden_throne"] == "1"
    assert row["workflow_state"] != "review_mode"
    assert main.scheduler.get_job(f"golden-throne-{iid}") is not None


def test_three_consecutive_gt_no_ops_force_review_mode_and_no_fourth_ping(app_env, monkeypatch):
    main = app_env.main
    monkeypatch.setattr(main.shared, "get_quiet_hours_status", lambda: {"active": False})
    client = TestClient(main.app)
    iid = _insert_gt(app_env.db_path, counter=2)

    resp = client.post(
        "/api/hooks/Stop",
        json={
            "session_id": iid,
            "transcript_tail": _assistant_tail("Still nothing actionable; ScheduleWakeup."),
        },
    )

    assert resp.status_code == 200, resp.text
    row = _row(app_env.db_path, iid)
    assert row["gt_no_op_counter"] == 3
    assert row["golden_throne"] is None
    assert row["workflow_state"] == "review_mode"
    assert row["status"] == "reviewing"
    assert main.scheduler.get_job(f"golden-throne-{iid}") is None
    evs = _events(app_env.db_path, iid)
    force = [e for e in evs if e["event_type"] == "sisyphus_force_close"]
    assert force, evs
    details = json.loads(force[-1]["details"])
    assert details["instance_id"] == iid
    assert details["count"] == 3
    assert len(details["last_3_response_summaries"]) == 3


def test_gt_response_with_tool_call_resets_no_op_counter(app_env, monkeypatch):
    main = app_env.main
    monkeypatch.setattr(main.shared, "get_quiet_hours_status", lambda: {"active": False})
    client = TestClient(main.app)
    iid = _insert_gt(app_env.db_path, counter=2)

    resp = client.post(
        "/api/hooks/Stop",
        json={"session_id": iid, "transcript_tail": _assistant_tool_tail("Bash")},
    )

    assert resp.status_code == 200, resp.text
    row = _row(app_env.db_path, iid)
    assert row["gt_no_op_counter"] == 0
    assert row["golden_throne"] == "1"
    assert main.scheduler.get_job(f"golden-throne-{iid}") is not None


def test_gt_response_with_git_delta_resets_no_op_counter(app_env, monkeypatch, tmp_path):
    main = app_env.main
    monkeypatch.setattr(main.shared, "get_quiet_hours_status", lambda: {"active": False})
    repo = _make_git_repo(tmp_path)
    before = worktree_fingerprint(str(repo))
    (repo / "file.txt").write_text("after\n", encoding="utf-8")
    client = TestClient(main.app)
    iid = _insert_gt(app_env.db_path, counter=2, fingerprint=before, working_dir=str(repo))

    resp = client.post(
        "/api/hooks/Stop",
        json={
            "session_id": iid,
            "transcript_tail": _assistant_tail("Updated the working tree."),
        },
    )

    assert resp.status_code == 200, resp.text
    row = _row(app_env.db_path, iid)
    assert row["gt_no_op_counter"] == 0
    assert row["golden_throne"] == "1"
    assert main.scheduler.get_job(f"golden-throne-{iid}") is not None


def test_gt_force_review_does_not_depend_on_victory_endpoints(app_env, monkeypatch):
    main = app_env.main
    monkeypatch.setattr(main.shared, "get_quiet_hours_status", lambda: {"active": False})

    async def victory_core_500(*_args, **_kwargs):  # would fail if the force path used it
        raise RuntimeError("victory endpoint 500")

    monkeypatch.setattr(main, "_victory_ack_core", victory_core_500)
    client = TestClient(main.app)
    iid = _insert_gt(app_env.db_path, counter=2)

    resp = client.post(
        "/api/hooks/Stop",
        json={
            "session_id": iid,
            "transcript_tail": _assistant_tail("Nothing left for me to do."),
        },
    )

    assert resp.status_code == 200, resp.text
    row = _row(app_env.db_path, iid)
    assert row["golden_throne"] is None
    assert row["workflow_state"] == "review_mode"
    assert main.scheduler.get_job(f"golden-throne-{iid}") is None
