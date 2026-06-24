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
        """INSERT INTO legacy_instances
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
                    "TOKEN_API_LAUNCHER": "dispatch",
                    "TOKEN_API_ENGINE": "codex",
                },
            }
        )
        assert result["success"] is True

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    parent = conn.execute(
        "SELECT parent_instance_id FROM legacy_instances WHERE id = ?",
        ("child-session-start",),
    ).fetchone()[0]
    conn.close()
    assert parent is None


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


# --- UserPromptSubmit→commander poke fanout (sister to Stop→commander) ----------


def test_human_poke_to_commanded_worker_enqueues_fanout(app_env):
    # hook_driven defaults to 0 (genuine human poke); commanded via chapter edge.
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "fg-cmdr")
    _insert_instance(app_env.db_path, "worker-a", parent_instance_id="fg-cmdr")

    async def run():
        result = await hooks.handle_prompt_submit(
            {"session_id": "worker-a", "prompt": "rebase onto main and rerun pytest"}
        )
        assert result["success"] is True

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    rows = conn.execute(
        """SELECT audience_instance_id, source_instance_id, kind, payload_json, status
           FROM state_injections WHERE kind = 'worker_poked'"""
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "fg-cmdr"
    assert row[1] == "worker-a"
    assert row[2] == "worker_poked"
    assert row[4] == "pending"
    payload = json.loads(row[3])
    assert payload["child_instance_id"] == "worker-a"
    # Verbatim prompt text (decision #1) — no summary, no notice-only.
    assert payload["prompt_text"] == "rebase onto main and rerun pytest"


def test_system_induced_prompt_does_not_enqueue_poke(app_env):
    # The self-notify-loop guard: hook_driven=1 means an automated wake preceded
    # this prompt (FG talk/brief, state fanout) → NOT a human poke → no notify.
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "fg-cmdr2")
    _insert_instance(app_env.db_path, "worker-b", parent_instance_id="fg-cmdr2")
    conn = sqlite3.connect(app_env.db_path)
    conn.execute("UPDATE instances SET hook_driven = 1 WHERE id = ?", ("worker-b",))
    conn.commit()
    conn.close()

    async def run():
        result = await hooks.handle_prompt_submit(
            {"session_id": "worker-b", "prompt": "this followed an automated wake"}
        )
        assert result["success"] is True

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM state_injections WHERE kind = 'worker_poked'"
    ).fetchone()[0]
    conn.close()
    assert count == 0


def test_uncommanded_worker_no_poke(app_env):
    # No parent → commander_type 'emperor' → uncommanded → no-op.
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "solo")

    async def run():
        result = await hooks.handle_prompt_submit(
            {"session_id": "solo", "prompt": "just a normal prompt"}
        )
        assert result["success"] is True

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM state_injections WHERE kind = 'worker_poked'"
    ).fetchone()[0]
    conn.close()
    assert count == 0


def test_empty_prompt_no_poke(app_env):
    # An empty / resume UserPromptSubmit (no prompt text) is not a real poke.
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "fg-cmdr3")
    _insert_instance(app_env.db_path, "worker-c", parent_instance_id="fg-cmdr3")

    async def run():
        result = await hooks.handle_prompt_submit({"session_id": "worker-c"})
        assert result["success"] is True

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM state_injections WHERE kind = 'worker_poked'"
    ).fetchone()[0]
    conn.close()
    assert count == 0


def test_commander_consumes_worker_poked(app_env):
    # The commander surfaces the poke on its next UserPromptSubmit via the generic
    # _consume_state_injections path, identical to child_stopped.
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "fg-cmdr4")
    _insert_instance(app_env.db_path, "worker-d", parent_instance_id="fg-cmdr4")

    async def run():
        poke = await hooks.handle_prompt_submit(
            {"session_id": "worker-d", "prompt": "ship the hotfix"}
        )
        assert poke["success"] is True
        consume = await hooks.handle_prompt_submit({"session_id": "fg-cmdr4"})
        assert consume["success"] is True
        assert len(consume["state_injections"]) == 1
        assert consume["state_injections"][0]["kind"] == "worker_poked"
        # Names the worker (descriptive, never a raw pane id) + verbatim prompt.
        assert "worker-d" in consume["system_reminder"]
        assert "ship the hotfix" in consume["system_reminder"]

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    status = conn.execute(
        "SELECT status FROM state_injections WHERE kind = 'worker_poked'"
    ).fetchone()[0]
    conn.close()
    assert status == "consumed"
