import asyncio
import json
import sqlite3
import sys


def _insert_instance(
    db_path,
    instance_id: str,
    *,
    parent_instance_id: str | None = None,
    is_subagent: int = 0,
    session_doc_id: int | None = None,
):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id, status,
            is_subagent, parent_instance_id, session_doc_id, instance_type)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', 'idle', ?, ?, ?, 'one_off')""",
        (
            instance_id,
            instance_id,
            instance_id,
            "/tmp",
            is_subagent,
            parent_instance_id,
            session_doc_id,
        ),
    )
    conn.commit()
    conn.close()


def test_session_start_captures_parent_instance_id(app_env):
    hooks = sys.modules["routes.hooks"]

    async def run():
        result = await hooks.handle_session_start(
            {
                "session_id": "child-session-start",
                "cwd": "/tmp",
                "env": {
                    "TOKEN_API_PARENT_INSTANCE_ID": "parent-abc",
                    "TOKEN_API_LAUNCHER": "vault-dispatch",
                    "TOKEN_API_ENGINE": "codex",
                },
            }
        )
        assert result["success"] is True

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    parent = conn.execute(
        "SELECT parent_instance_id FROM claude_instances WHERE id = ?",
        ("child-session-start",),
    ).fetchone()[0]
    conn.close()
    assert parent == "parent-abc"


def test_child_stop_enqueues_injection_for_parent(app_env):
    hooks = sys.modules["routes.hooks"]
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        "INSERT INTO session_documents (id, file_path, title) VALUES (42, ?, ?)",
        ("Mars/Sessions/child.md", "child"),
    )
    conn.commit()
    conn.close()
    _insert_instance(app_env.db_path, "parent-1")
    _insert_instance(
        app_env.db_path,
        "child-1",
        parent_instance_id="parent-1",
        is_subagent=1,
        session_doc_id=42,
    )

    async def run():
        result = await hooks.handle_stop({"session_id": "child-1", "exit_code": 0})
        assert result["success"] is True
        assert result["parent_fanout"]["audience_instance_id"] == "parent-1"

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute(
        """SELECT audience_instance_id, source_instance_id, kind, payload_json, status
           FROM state_injections"""
    ).fetchone()
    conn.close()
    assert row[0] == "parent-1"
    assert row[1] == "child-1"
    assert row[2] == "child_stopped"
    assert row[4] == "pending"
    payload = json.loads(row[3])
    assert payload["child_instance_id"] == "child-1"
    assert payload["child_session_doc_id"] == 42
    assert payload["child_session_doc_path"] == "Mars/Sessions/child.md"
    assert payload["exit_reason"] == "normal"


def test_parent_prompt_submit_consumes_pending_injection(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "parent-2")
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO state_injections
           (audience_instance_id, source_instance_id, kind, payload_json, rendered_text)
           VALUES (?, ?, ?, ?, ?)""",
        (
            "parent-2",
            "child-2",
            "child_stopped",
            json.dumps({"kind": "child_stopped", "child_instance_id": "child-2"}),
            "<system-reminder>\nA dispatched child instance stopped.\n</system-reminder>",
        ),
    )
    conn.commit()
    conn.close()

    async def run():
        result = await hooks.handle_prompt_submit({"session_id": "parent-2"})
        assert result["success"] is True
        assert len(result["state_injections"]) == 1
        assert "A dispatched child instance stopped." in result["system_reminder"]
        assert result["hookSpecificOutput"]["additionalContext"] == result["system_reminder"]

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    status = conn.execute("SELECT status FROM state_injections").fetchone()[0]
    conn.close()
    assert status == "consumed"
