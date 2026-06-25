import asyncio
import json
import sqlite3
import sys


def _fg_persona_id() -> str:
    from personas import persona_id_for_slug

    return persona_id_for_slug("fabricator-general")


def _insert_real_instance(
    db_path,
    instance_id: str,
    *,
    commander_type: str = "emperor",
    commander_id: str | None = None,
    persona_id: str | None = None,
    rank: str = "astartes",
    status: str = "idle",
    hook_driven: int = 0,
):
    """Insert directly into the real ``instances`` table.

    The conftest ``legacy_instances`` view can't express ``commander_type='persona'``
    or a non-astartes rank, so the persona-edge fixtures bypass it (mirrors the
    ``UPDATE instances SET hook_driven=1`` precedent in this file).
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO instances
           (id, name, working_dir, device_id, origin_type, commander_type,
            commander_id, persona_id, rank, status, hook_driven)
           VALUES (?, ?, '/tmp', 'Mac-Mini', 'local', ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            instance_id,
            commander_type,
            commander_id,
            persona_id,
            rank,
            status,
            hook_driven,
        ),
    )
    conn.commit()
    conn.close()


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


def test_human_poke_to_commanded_worker_enqueues_fanout(app_env) -> None:
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


def test_system_induced_prompt_does_not_enqueue_poke(app_env) -> None:
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


def test_uncommanded_worker_no_poke(app_env) -> None:
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


def test_empty_prompt_no_poke(app_env) -> None:
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


def test_commander_consumes_worker_poked(app_env) -> None:
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


# --- Persona commander edge: fanout resolves FG/Custodes, not just chapter ------
# A normally-dispatched FG worker carries commander_type='persona',
# commander_id=<fabricator-general persona row id> ("report to THE current FG",
# resolved at delivery time). The injection is addressed to the durable persona id;
# the live FG singleton drains injections addressed to its own persona_id at consume
# time — staleness-proof across singleton restarts.


def test_persona_worker_poke_addresses_persona(app_env) -> None:
    # Producer: a persona-edge poke addresses the durable persona id (= commander_id).
    hooks = sys.modules["routes.hooks"]
    fg_persona = _fg_persona_id()
    _insert_real_instance(
        app_env.db_path,
        "fg-worker-1",
        commander_type="persona",
        commander_id=fg_persona,
        persona_id=None,
    )

    async def run():
        result = await hooks.handle_prompt_submit(
            {"session_id": "fg-worker-1", "prompt": "bisect the regression"}
        )
        assert result["success"] is True

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    rows = conn.execute(
        """SELECT audience_instance_id, source_instance_id, payload_json
           FROM state_injections WHERE kind = 'worker_poked'"""
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == fg_persona
    assert rows[0][1] == "fg-worker-1"
    assert json.loads(rows[0][2])["prompt_text"] == "bisect the regression"


def test_persona_worker_stop_addresses_persona(app_env) -> None:
    # Producer: a persona-edge child_stopped also addresses the durable persona id.
    hooks = sys.modules["routes.hooks"]
    fg_persona = _fg_persona_id()
    _insert_real_instance(
        app_env.db_path,
        "fg-worker-2",
        commander_type="persona",
        commander_id=fg_persona,
        persona_id=None,
    )

    async def run():
        result = await hooks.handle_stop({"session_id": "fg-worker-2", "exit_code": 0})
        assert result["success"] is True
        assert result["parent_fanout"]["audience_instance_id"] == fg_persona

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute(
        """SELECT audience_instance_id, kind, status
           FROM state_injections WHERE kind = 'child_stopped'"""
    ).fetchone()
    conn.close()
    assert row[0] == fg_persona
    assert row[2] == "pending"


def test_live_fg_singleton_consumes_persona_addressed(app_env) -> None:
    # Consumer: the live FG singleton drains an injection addressed to its persona_id.
    hooks = sys.modules["routes.hooks"]
    fg_persona = _fg_persona_id()
    _insert_real_instance(
        app_env.db_path,
        "fg-singleton",
        commander_type="emperor",
        persona_id=fg_persona,
        rank="overseer",
        status="working",
    )
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO state_injections
           (audience_instance_id, source_instance_id, kind, payload_json, rendered_text)
           VALUES (?, ?, ?, ?, ?)""",
        (
            fg_persona,
            "fg-worker-3",
            "worker_poked",
            json.dumps({"kind": "worker_poked", "child_instance_id": "fg-worker-3"}),
            '<system-reminder>\n⬆ Emperor poked fg-worker-3: "ship it"\n</system-reminder>',
        ),
    )
    conn.commit()
    conn.close()

    async def run():
        result = await hooks.handle_prompt_submit({"session_id": "fg-singleton"})
        assert result["success"] is True
        assert len(result["state_injections"]) == 1
        assert "fg-worker-3" in result["system_reminder"]

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    status = conn.execute("SELECT status FROM state_injections").fetchone()[0]
    conn.close()
    assert status == "consumed"


def test_persona_addressed_survives_singleton_restart(app_env) -> None:
    # Durability: enqueue to the persona, the original singleton dies, a NEW FG
    # singleton comes up → the new instance consumes it (the point of the persona edge).
    hooks = sys.modules["routes.hooks"]
    fg_persona = _fg_persona_id()
    _insert_real_instance(
        app_env.db_path,
        "fg-old",
        commander_type="emperor",
        persona_id=fg_persona,
        rank="overseer",
        status="stopped",
    )
    _insert_real_instance(
        app_env.db_path,
        "fg-new",
        commander_type="emperor",
        persona_id=fg_persona,
        rank="overseer",
        status="working",
    )
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO state_injections
           (audience_instance_id, source_instance_id, kind, payload_json, rendered_text)
           VALUES (?, ?, ?, ?, ?)""",
        (
            fg_persona,
            "fg-worker-4",
            "worker_poked",
            json.dumps({"kind": "worker_poked", "child_instance_id": "fg-worker-4"}),
            "<system-reminder>\nworker_poked\n</system-reminder>",
        ),
    )
    conn.commit()
    conn.close()

    async def run():
        result = await hooks.handle_prompt_submit({"session_id": "fg-new"})
        assert result["success"] is True
        assert len(result["state_injections"]) == 1

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    status = conn.execute("SELECT status FROM state_injections").fetchone()[0]
    conn.close()
    assert status == "consumed"


def test_non_singleton_does_not_consume_persona_addressed(app_env) -> None:
    # Neither a normal FG-commanded worker (its OWN persona_id is NULL), nor a
    # chapter subagent that shares the FG persona_id, may drain a persona-addressed
    # injection — only the live singleton (resolved by rank + commander_type!='chapter')
    # does. NB: a non-chapter row carrying persona_id=fg_persona can't model a "plain
    # astartes worker" — trg_instances_stamp_persona_rank force-promotes it to FG's
    # default overseer rank, i.e. it BECOMES the singleton. The realizable non-singletons
    # are: (a) a worker whose own persona_id is NULL, (b) a chapter subagent.
    hooks = sys.modules["routes.hooks"]
    fg_persona = _fg_persona_id()
    _insert_real_instance(
        app_env.db_path,
        "fg-worker",
        commander_type="persona",
        commander_id=fg_persona,
        persona_id=None,
        rank="astartes",
    )
    # The chapter subagent needs an active parent instance (DB trigger
    # trg_instances_chapter_persona_guard); commander_type='chapter' both keeps its
    # persona_id from promoting it AND is exactly what resolve_live_persona_instance
    # (and the consume gate) filter out.
    _insert_real_instance(app_env.db_path, "fg-chapter-parent", commander_type="emperor")
    _insert_real_instance(
        app_env.db_path,
        "fg-chapter-child",
        commander_type="chapter",
        commander_id="fg-chapter-parent",
        persona_id=fg_persona,
        rank="overseer",
    )
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO state_injections
           (audience_instance_id, source_instance_id, kind, payload_json, rendered_text)
           VALUES (?, ?, ?, ?, ?)""",
        (
            fg_persona,
            "fg-worker-5",
            "worker_poked",
            json.dumps({"kind": "worker_poked", "child_instance_id": "fg-worker-5"}),
            "<system-reminder>\nworker_poked\n</system-reminder>",
        ),
    )
    conn.commit()
    conn.close()

    async def run():
        for sid in ("fg-worker", "fg-chapter-child"):
            result = await hooks.handle_prompt_submit({"session_id": sid})
            assert result["success"] is True

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    status = conn.execute("SELECT status FROM state_injections").fetchone()[0]
    conn.close()
    assert status == "pending"


def test_cross_persona_singleton_does_not_consume(app_env) -> None:
    # The administratum singleton must NOT drain an FG-addressed injection.
    hooks = sys.modules["routes.hooks"]
    from personas import persona_id_for_slug

    fg_persona = _fg_persona_id()
    admin_persona = persona_id_for_slug("administratum")
    _insert_real_instance(
        app_env.db_path,
        "admin-singleton",
        commander_type="emperor",
        persona_id=admin_persona,
        rank="overseer",
        status="working",
    )
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO state_injections
           (audience_instance_id, source_instance_id, kind, payload_json, rendered_text)
           VALUES (?, ?, ?, ?, ?)""",
        (
            fg_persona,
            "fg-worker-6",
            "worker_poked",
            json.dumps({"kind": "worker_poked", "child_instance_id": "fg-worker-6"}),
            "<system-reminder>\nworker_poked\n</system-reminder>",
        ),
    )
    conn.commit()
    conn.close()

    async def run():
        result = await hooks.handle_prompt_submit({"session_id": "admin-singleton"})
        assert result["success"] is True
        assert not result.get("state_injections")

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    status = conn.execute("SELECT status FROM state_injections").fetchone()[0]
    conn.close()
    assert status == "pending"


def test_chapter_edge_regression(app_env) -> None:
    # Chapter regression guard: chapter poke still routes to the instance-id parent,
    # and that parent consumes it via its own session_id (NOT a persona id).
    hooks = sys.modules["routes.hooks"]
    _insert_real_instance(app_env.db_path, "chap-parent", commander_type="emperor")
    _insert_real_instance(
        app_env.db_path,
        "chap-worker",
        commander_type="chapter",
        commander_id="chap-parent",
    )

    async def run():
        poke = await hooks.handle_prompt_submit(
            {"session_id": "chap-worker", "prompt": "rerun the suite"}
        )
        assert poke["success"] is True
        consume = await hooks.handle_prompt_submit({"session_id": "chap-parent"})
        assert consume["success"] is True
        assert len(consume["state_injections"]) == 1
        assert "rerun the suite" in consume["system_reminder"]

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute(
        "SELECT audience_instance_id, status FROM state_injections WHERE kind = 'worker_poked'"
    ).fetchone()
    conn.close()
    assert row[0] == "chap-parent"
    assert row[1] == "consumed"


def test_self_loop_guard_persona_self_commanded(app_env) -> None:
    # A singleton commanded by its own persona (commander_id == persona_id) must not
    # enqueue a notice to itself.
    hooks = sys.modules["routes.hooks"]
    fg_persona = _fg_persona_id()
    _insert_real_instance(
        app_env.db_path,
        "fg-self",
        commander_type="persona",
        commander_id=fg_persona,
        persona_id=fg_persona,
        rank="overseer",
        status="working",
    )

    async def run():
        result = await hooks.handle_prompt_submit(
            {"session_id": "fg-self", "prompt": "a genuine human poke"}
        )
        assert result["success"] is True

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    count = conn.execute("SELECT COUNT(*) FROM state_injections").fetchone()[0]
    conn.close()
    assert count == 0
