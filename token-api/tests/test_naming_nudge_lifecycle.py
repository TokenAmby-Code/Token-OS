from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

from instance_mutation import sanctioned_insert_instance_sync, sanctioned_update_instance_sync


def _insert_wrapper_instance(
    db_path,
    *,
    instance_id="wrap-unnamed",
    wrapper_id="wrap-1",
    name="needs-name",
    session_doc_id=None,
) -> None:
    now = datetime.now().isoformat()
    with sqlite3.connect(db_path) as conn:
        sanctioned_insert_instance_sync(
            conn,
            values={
                "id": instance_id,
                "name": "needs-name",
                "engine": "codex",
                "working_dir": "/tmp",
                "device_id": "Mac-Mini",
                "status": "working",
                "rank": "astartes",
                "wrapper_launch_id": wrapper_id,
                "session_doc_id": session_doc_id,
                "created_at": now,
                "last_activity": now,
            },
            mutation_type="instance_registered",
            write_source="test",
            actor="test",
        )
        if name != "needs-name":
            sanctioned_update_instance_sync(
                conn,
                instance_id=instance_id,
                updates={"name": name},
                mutation_type="instance_updated",
                write_source="test",
                actor="instance-name-cli",
            )
        conn.commit()


@pytest.mark.asyncio
async def test_prompt_submit_on_placeholder_row_schedules_naming_nudge(
    app_env, monkeypatch
) -> None:
    """Normal naming interview is scheduled after the first prompt-submit commit."""
    hooks = sys.modules["routes.hooks"]
    _insert_wrapper_instance(app_env.db_path, instance_id="prompt-unnamed", wrapper_id="wrap-p")

    scheduled: list[tuple[str | None, str]] = []
    monkeypatch.setattr(
        hooks, "_schedule_naming_nudge", lambda iid, source: scheduled.append((iid, source))
    )

    result = await hooks.handle_prompt_submit({"session_id": "prompt-unnamed"})

    assert result["action"] == "processing"
    assert scheduled == [("prompt-unnamed", "UserPromptSubmit")]
    with sqlite3.connect(app_env.db_path) as conn:
        assert conn.execute(
            """
            SELECT 1 FROM events
            WHERE instance_id = 'prompt-unnamed'
              AND event_type = 'hook_user_prompt_submit'
            """
        ).fetchone()


@pytest.mark.asyncio
async def test_prompt_submit_on_named_row_does_not_schedule_naming_nudge(
    app_env, monkeypatch
) -> None:
    """Already named panes must not get a rename interview on prompt submit."""
    hooks = sys.modules["routes.hooks"]
    _insert_wrapper_instance(
        app_env.db_path,
        instance_id="prompt-named",
        wrapper_id="wrap-named",
        name="active-naming-hook",
    )

    scheduled: list[tuple[str | None, str]] = []
    monkeypatch.setattr(
        hooks, "_schedule_naming_nudge", lambda iid, source: scheduled.append((iid, source))
    )

    result = await hooks.handle_prompt_submit({"session_id": "prompt-named"})

    assert result["action"] == "processing"
    assert scheduled == []


@pytest.mark.asyncio
async def test_session_start_does_not_schedule_naming_nudge(app_env, monkeypatch) -> None:
    """SessionStart only registers/stamps; naming waits for the first real prompt."""
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(
        hooks,
        "_schedule_naming_nudge",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("SessionStart must not schedule naming nudges")
        ),
    )

    result = await hooks.handle_session_start(
        {
            "session_id": "sess-needs-name",
            "cwd": "/tmp",
            "pid": 1001,
            "env": {"TMUX_PANE": "%42", "TOKEN_API_ENGINE": "claude"},
        }
    )

    assert result["action"] == "registered"


@pytest.mark.asyncio
async def test_wrapper_end_does_not_schedule_naming_nudge(app_env, monkeypatch) -> None:
    """WrapperEnd is terminal cleanup only; no post-exit rename prompt."""
    hooks = sys.modules["routes.hooks"]
    _insert_wrapper_instance(app_env.db_path)

    monkeypatch.setattr(
        hooks,
        "_schedule_naming_nudge",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("WrapperEnd must not schedule naming nudges")
        ),
    )
    monkeypatch.setattr(hooks.shared, "clear_pane_tint", lambda *a, **k: None)

    result = await hooks.handle_wrapper_end(
        {
            "wrapper_launch_id": "wrap-1",
            "engine": "codex",
            "launcher": "codex-wrapper",
            "tmux_pane": "%9",
            "env": {"TOKEN_API_WRAPPER_LAUNCH_ID": "wrap-1", "TMUX_PANE": "%9"},
        }
    )

    assert result["action"] == "wrapper_end_stopped_instance"


@pytest.mark.asyncio
async def test_session_end_does_not_schedule_naming_nudge(app_env, monkeypatch) -> None:
    """SessionEnd must not bury the real exit blurb with a naming interview."""
    hooks = sys.modules["routes.hooks"]
    _insert_wrapper_instance(app_env.db_path, instance_id="sess-unnamed", wrapper_id="wrap-2")

    monkeypatch.setattr(
        hooks,
        "_schedule_naming_nudge",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("SessionEnd must not schedule naming nudges")
        ),
    )
    monkeypatch.setattr(hooks.shared, "clear_pane_tint", lambda *a, **k: None)
    monkeypatch.setattr(hooks, "_spawn_session_end_assertion", lambda *a, **k: None)

    result = await hooks.handle_session_end(
        {"session_id": "sess-unnamed", "wrapper_launch_id": "wrap-2", "reason": "logout"}
    )

    assert result["action"] == "stopped"


@pytest.mark.asyncio
async def test_stop_does_not_schedule_naming_nudge(app_env, monkeypatch) -> None:
    """Stop must not bury the real exit blurb with a naming interview."""
    hooks = sys.modules["routes.hooks"]
    _insert_wrapper_instance(app_env.db_path, instance_id="stop-unnamed", wrapper_id="wrap-stop")
    with sqlite3.connect(app_env.db_path) as conn:
        sanctioned_update_instance_sync(
            conn,
            instance_id="stop-unnamed",
            updates={"is_subagent": 1},
            mutation_type="instance_updated",
            write_source="test",
            actor="test",
        )
        conn.commit()

    monkeypatch.setattr(
        hooks,
        "_schedule_naming_nudge",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Stop must not schedule naming nudges")
        ),
    )

    result = await hooks.handle_stop({"session_id": "stop-unnamed", "pid": 1003})

    assert result["action"] == "stop_processed_subagent"


@pytest.mark.asyncio
async def test_reconciler_schedules_live_placeholder_with_prompt_evidence(
    app_env, monkeypatch
) -> None:
    """Daemon backstop nudges live placeholder rows only after a real prompt event."""
    main = app_env.main
    _insert_wrapper_instance(app_env.db_path, instance_id="reconcile-unnamed", wrapper_id="wrap-r")
    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute(
            "INSERT INTO events (event_type, instance_id) VALUES ('hook_user_prompt_submit', 'reconcile-unnamed')"
        )

    async def reachable_tmux():
        return {}

    async def live_ids():
        return {"reconcile-unnamed"}

    nudged: list[str] = []

    async def fake_maybe(instance_id):
        nudged.append(instance_id)
        return {"success": True, "action": "nudge_sent"}

    monkeypatch.setattr(main, "_read_tmux_panes", reachable_tmux)
    monkeypatch.setattr(main, "_live_agent_instance_ids", live_ids)
    monkeypatch.setattr(main, "_maybe_naming_nudge", fake_maybe)

    counts = await main._run_tmux_db_reconcile_cycle()
    await asyncio.sleep(0)

    assert counts["naming_nudge_scheduled"] == 1
    assert nudged == ["reconcile-unnamed"]


@pytest.mark.asyncio
async def test_reconciler_nudges_placeholder_with_session_doc(app_env, monkeypatch) -> None:
    """Live session-doc placeholder drift is flagged and interviewed."""
    main = app_env.main
    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute(
            "INSERT INTO session_documents (id, file_path, title, status) VALUES (7, 'Sessions/named-doc.md', 'Named Doc', 'active')"
        )
    _insert_wrapper_instance(
        app_env.db_path,
        instance_id="reconcile-doc-drift",
        wrapper_id="wrap-doc",
        session_doc_id=7,
    )
    with sqlite3.connect(app_env.db_path) as conn:
        sanctioned_update_instance_sync(
            conn,
            instance_id="reconcile-doc-drift",
            updates={"last_activity": (datetime.now() - timedelta(seconds=30)).isoformat()},
            mutation_type="instance_updated",
            write_source="test",
            actor="test",
        )
        conn.execute(
            "INSERT INTO events (event_type, instance_id) VALUES ('hook_user_prompt_submit', 'reconcile-doc-drift')"
        )

    async def reachable_tmux():
        return {}

    async def live_ids():
        return {"reconcile-doc-drift"}

    nudged = []

    async def fake_maybe(instance_id):
        nudged.append(instance_id)
        return {"success": True}

    monkeypatch.setattr(main, "_read_tmux_panes", reachable_tmux)
    monkeypatch.setattr(main, "_live_agent_instance_ids", live_ids)
    monkeypatch.setattr(main, "_maybe_naming_nudge", fake_maybe)

    counts = await main._run_tmux_db_reconcile_cycle()
    await asyncio.sleep(0)

    assert counts["naming_nudge_scheduled"] == 1
    assert nudged == ["reconcile-doc-drift"]
    assert counts["placeholder_tab_name_drift"] == 1


@pytest.mark.asyncio
async def test_reconciler_does_not_reschedule_after_naming_nudge_sent(app_env, monkeypatch) -> None:
    """Backstop is once-only after a sent naming nudge; the core handles repeats."""
    main = app_env.main
    _insert_wrapper_instance(app_env.db_path, instance_id="reconcile-nudged", wrapper_id="wrap-n")
    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute(
            "INSERT INTO events (event_type, instance_id) VALUES ('hook_user_prompt_submit', 'reconcile-nudged')"
        )
        conn.execute(
            "INSERT INTO events (event_type, instance_id) VALUES ('naming_nudge_sent', 'reconcile-nudged')"
        )

    async def reachable_tmux():
        return {}

    async def live_ids():
        return {"reconcile-nudged"}

    async def fail_maybe(instance_id):
        raise AssertionError(f"unexpected naming nudge: {instance_id}")

    monkeypatch.setattr(main, "_read_tmux_panes", reachable_tmux)
    monkeypatch.setattr(main, "_live_agent_instance_ids", live_ids)
    monkeypatch.setattr(main, "_maybe_naming_nudge", fail_maybe)

    counts = await main._run_tmux_db_reconcile_cycle()
    await asyncio.sleep(0)

    assert counts["naming_nudge_scheduled"] == 0


@pytest.mark.asyncio
async def test_reconciler_does_not_nudge_live_placeholder_without_prompt_evidence(
    app_env, monkeypatch
) -> None:
    """A merely opened placeholder pane is not interviewed until UserPromptSubmit."""
    main = app_env.main
    _insert_wrapper_instance(app_env.db_path, instance_id="reconcile-quiet", wrapper_id="wrap-q")

    async def reachable_tmux():
        return {}

    async def live_ids():
        return {"reconcile-quiet"}

    async def fail_maybe(instance_id):
        raise AssertionError(f"unexpected naming nudge: {instance_id}")

    monkeypatch.setattr(main, "_read_tmux_panes", reachable_tmux)
    monkeypatch.setattr(main, "_live_agent_instance_ids", live_ids)
    monkeypatch.setattr(main, "_maybe_naming_nudge", fail_maybe)

    counts = await main._run_tmux_db_reconcile_cycle()
    await asyncio.sleep(0)

    assert counts["naming_nudge_scheduled"] == 0


@pytest.mark.asyncio
async def test_reconciler_resets_unofficial_date_name_and_interviews(app_env, monkeypatch) -> None:
    """Illegal non-provenanced names are reset to needs-name and interviewed."""
    main = app_env.main
    _insert_wrapper_instance(
        app_env.db_path,
        instance_id="illegal-date-name",
        wrapper_id="wrap-date",
    )
    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute("UPDATE instances SET name = '2026-06-25' WHERE id = 'illegal-date-name'")
        conn.commit()

    async def reachable_tmux():
        return {}

    async def live_ids():
        return {"illegal-date-name"}

    nudged = []

    async def fake_maybe(instance_id):
        nudged.append(instance_id)
        return {"success": True}

    monkeypatch.setattr(main, "_read_tmux_panes", reachable_tmux)
    monkeypatch.setattr(main, "_live_agent_instance_ids", live_ids)
    monkeypatch.setattr(main, "_maybe_naming_nudge", fake_maybe)

    counts = await main._run_tmux_db_reconcile_cycle()
    await asyncio.sleep(0)

    assert counts["illegal_instance_name_reset"] == 1
    assert counts["naming_nudge_scheduled"] == 1
    assert nudged == ["illegal-date-name"]
    with sqlite3.connect(app_env.db_path) as conn:
        row = conn.execute(
            "SELECT name, workflow_blocked_reason FROM instances WHERE id = 'illegal-date-name'"
        ).fetchone()
    assert row == ("needs-name", "tab_name_placeholder")


@pytest.mark.asyncio
async def test_reconciler_preserves_officially_renamed_row(app_env, monkeypatch) -> None:
    """A non-placeholder name with official rename provenance is untouched."""
    main = app_env.main
    _insert_wrapper_instance(
        app_env.db_path,
        instance_id="official-name",
        wrapper_id="wrap-official",
        name="official-human-name",
    )

    async def reachable_tmux():
        return {}

    async def live_ids():
        return {"official-name"}

    monkeypatch.setattr(main, "_read_tmux_panes", reachable_tmux)
    monkeypatch.setattr(main, "_live_agent_instance_ids", live_ids)
    monkeypatch.setattr(
        main,
        "_maybe_naming_nudge",
        lambda instance_id: (_ for _ in ()).throw(AssertionError(instance_id)),
    )

    counts = await main._run_tmux_db_reconcile_cycle()
    await asyncio.sleep(0)

    assert counts["illegal_instance_name_reset"] == 0
    assert counts["naming_nudge_scheduled"] == 0
    with sqlite3.connect(app_env.db_path) as conn:
        row = conn.execute("SELECT name FROM instances WHERE id = 'official-name'").fetchone()
    assert row == ("official-human-name",)


@pytest.mark.asyncio
async def test_codex_one_off_session_end_preserves_instance_stamp_resolution(
    app_env, monkeypatch
) -> None:
    """Completed Codex one-shots must keep the pane @INSTANCE_ID resolvable.

    Regression: terminal SessionEnd spawned assert-instance; because the Codex
    process had exited, stack-worker assertion pruned/cleared the pane stamp, so
    tmuxctl resolve-instance failed immediately after completion.
    """
    hooks = sys.modules["routes.hooks"]
    now = datetime.now().isoformat()
    with sqlite3.connect(app_env.db_path) as conn:
        # tmux_pane/pane_label are runtime ids the sanctioned writer rejects; the
        # one-off classification this test exercises reads engine/golden_throne/
        # hook_driven only, so seed via the sanctioned helper without them.
        sanctioned_insert_instance_sync(
            conn,
            values={
                "id": "codex-done",
                "name": "done",
                "engine": "codex",
                "working_dir": "/tmp",
                "device_id": "Mac-Mini",
                "status": "working",
                "rank": "astartes",
                "wrapper_launch_id": "wrap-done",
                "golden_throne": None,
                "hook_driven": 0,
                "created_at": now,
                "last_activity": now,
            },
            mutation_type="instance_registered",
            write_source="test",
            actor="test",
        )
        conn.commit()

    spawned: list[tuple[str, str]] = []
    monkeypatch.setattr(hooks, "_spawn_session_end_assertion", lambda *a: spawned.append(a))
    monkeypatch.setattr(hooks, "_schedule_naming_nudge", lambda *a, **k: None)
    monkeypatch.setattr(hooks.shared, "clear_pane_tint", lambda *a, **k: None)

    result = await hooks.handle_session_end(
        {
            "session_id": "codex-done",
            "wrapper_launch_id": "wrap-done",
            "engine": "codex",
            "tmux_pane": "%9",
            "env": {"TOKEN_API_INSTANCE_TYPE": "one_off"},
        }
    )

    assert result["action"] == "stopped"
    assert spawned == []
