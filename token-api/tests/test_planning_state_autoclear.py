"""Event-driven planning_state auto-clear (phase 1 of the plan-watchdog work).

These pin the reliable-exit guarantees that replace the screen-scrape watcher's
10s-timeout race as the authority for the planning→none transition:

  * handle_post_tool_use: the first mutating tool (Write/Edit/MultiEdit/
    NotebookEdit) after a plan is approved clears planning_state to `none`
    (source `auto-clear:tool-exec`) — poll-free, before the 2s debounce.
  * The clear is CAS-gated to only_if_in=(planning, approving): it preserves
    `preplanning` (a /preplan session-doc edit must not false-clear) and no-ops on
    `none`; Bash and read tools (which fire freely in plan mode) never clear.
  * handle_session_start reconciles a stuck planning_state on re-registration
    (source `auto-clear:session-start`) — a resumed session is never mid-modal.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys


def _insert_instance(
    db_path,
    instance_id,
    *,
    pane=None,
    planning_state="none",
    status="idle",
    legion="astartes",
    engine=None,
    wrapper_launch_id=None,
):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            profile_name, tts_voice, notification_sound, status, tmux_pane,
            legion, planning_state, engine, wrapper_launch_id)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', 'p', 'v', 's', ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            f"{instance_id}-session",
            instance_id,
            "/tmp",
            status,
            pane,
            legion,
            planning_state,
            engine,
            wrapper_launch_id,
        ),
    )
    if wrapper_launch_id:
        conn.execute(
            "UPDATE instances SET wrapper_launch_id = ? WHERE id = ?",
            (wrapper_launch_id, instance_id),
        )
    conn.commit()
    conn.close()


def _planning(db_path, instance_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT planning_state, planning_source FROM legacy_instances WHERE id = ?",
        (instance_id,),
    ).fetchone()
    conn.close()
    return (row[0], row[1]) if row else (None, None)


def _planning_mutations(db_path, instance_id) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        """SELECT COUNT(*) FROM instance_mutations
           WHERE instance_id = ? AND mutation_type = 'planning_state_changed'""",
        (instance_id,),
    ).fetchone()[0]
    conn.close()
    return n


async def _never_dead(db, session_id, existing, actor):
    return False


def _post_tool(app_env, monkeypatch, session_id, tool_name):
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_stop_if_dead_pane", _never_dead)

    async def run():
        return await hooks.handle_post_tool_use({"session_id": session_id, "tool_name": tool_name})

    return asyncio.run(run())


def _prompt_submit(app_env, monkeypatch, payload):
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_stop_if_dead_pane", _never_dead)

    async def run():
        return await hooks.handle_prompt_submit(payload)

    return asyncio.run(run())


# ── Mutating tools clear an open planning modal ────────────────────────────────


def test_write_clears_planning(app_env, monkeypatch):
    _insert_instance(app_env.db_path, "plan-1", pane="%40", planning_state="planning")
    _post_tool(app_env, monkeypatch, "plan-1", "Write")
    state, source = _planning(app_env.db_path, "plan-1")
    assert state == "none"
    assert source == "auto-clear:tool-exec"
    assert _planning_mutations(app_env.db_path, "plan-1") == 1


def test_edit_clears_planning(app_env, monkeypatch):
    _insert_instance(app_env.db_path, "plan-2", pane="%41", planning_state="planning")
    _post_tool(app_env, monkeypatch, "plan-2", "Edit")
    state, source = _planning(app_env.db_path, "plan-2")
    assert state == "none"
    assert source == "auto-clear:tool-exec"
    assert _planning_mutations(app_env.db_path, "plan-2") == 1


def test_write_clears_approving(app_env, monkeypatch):
    # The watcher leaves `approving` on timeout (change #4); the post-approval
    # Write resolves it.
    _insert_instance(app_env.db_path, "plan-3", pane="%42", planning_state="approving")
    _post_tool(app_env, monkeypatch, "plan-3", "Write")
    state, source = _planning(app_env.db_path, "plan-3")
    assert state == "none"
    assert source == "auto-clear:tool-exec"


# ── The gate protects preplanning and no-ops on none ───────────────────────────


def test_write_preserves_preplanning(app_env, monkeypatch):
    # A /preplan session-doc edit happens while preplanning — must NOT false-clear.
    _insert_instance(app_env.db_path, "pre-1", pane="%43", planning_state="preplanning")
    _post_tool(app_env, monkeypatch, "pre-1", "Write")
    state, _ = _planning(app_env.db_path, "pre-1")
    assert state == "preplanning"
    assert _planning_mutations(app_env.db_path, "pre-1") == 0


def test_bash_does_not_clear_planning(app_env, monkeypatch):
    # Bash runs freely in plan mode — not a "planning ended" signal.
    _insert_instance(app_env.db_path, "plan-4", pane="%44", planning_state="planning")
    _post_tool(app_env, monkeypatch, "plan-4", "Bash")
    state, _ = _planning(app_env.db_path, "plan-4")
    assert state == "planning"
    assert _planning_mutations(app_env.db_path, "plan-4") == 0


def test_write_on_none_noops(app_env, monkeypatch):
    _insert_instance(app_env.db_path, "idle-1", pane="%45", planning_state="none")
    _post_tool(app_env, monkeypatch, "idle-1", "Write")
    state, _ = _planning(app_env.db_path, "idle-1")
    assert state == "none"
    assert _planning_mutations(app_env.db_path, "idle-1") == 0


# ── The clear runs before the debounce ─────────────────────────────────────────


def test_write_clears_even_when_debounced(app_env, monkeypatch):
    # Prime the debounce so handle_post_tool_use early-returns "debounced". The
    # planning clear is placed BEFORE the debounce, so it must still fire — this
    # guards against a prior tool's debounce window swallowing the approval edit.
    import time

    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "plan-5", pane="%46", planning_state="planning")
    hooks._post_tool_debounce["plan-5"] = time.time()

    res = _post_tool(app_env, monkeypatch, "plan-5", "Write")
    assert res["action"] == "debounced"
    state, source = _planning(app_env.db_path, "plan-5")
    assert state == "none"
    assert source == "auto-clear:tool-exec"


# ── Prompt submit arms planning state ──────────────────────────────────────────


def test_codex_prompt_submit_plan_context_sets_planning(app_env, monkeypatch):
    _insert_instance(app_env.db_path, "codex-plan-1", pane="%48", engine="codex")

    res = _prompt_submit(
        app_env,
        monkeypatch,
        {
            "session_id": "codex-plan-1",
            "turn_context": {"mode": "plan"},
            "prompt": "implement the approved design",
        },
    )

    assert res["action"] == "processing"
    state, source = _planning(app_env.db_path, "codex-plan-1")
    assert state == "planning"
    assert source == "auto-clear:prompt-submit"


def test_slash_plan_prompt_sets_planning_for_any_harness(app_env, monkeypatch):
    _insert_instance(app_env.db_path, "slash-plan-1", pane="%49", engine="claude")

    _prompt_submit(app_env, monkeypatch, {"session_id": "slash-plan-1", "prompt": "  /plan fix it"})

    state, source = _planning(app_env.db_path, "slash-plan-1")
    assert state == "planning"
    assert source == "auto-clear:prompt-submit"



def _subscription(db_path, instance_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        """SELECT status, purpose, payload, oneshot, target_pane, subscriber_pane
           FROM stop_hook_subscriptions
           WHERE target_instance_id = ?""",
        (instance_id,),
    ).fetchone()
    conn.close()
    return row


def test_slash_preplan_prompt_sets_preplanning_and_arms_plan_followup(app_env, monkeypatch):
    _insert_instance(app_env.db_path, "slash-preplan-1", pane="%54", engine="claude")

    _prompt_submit(app_env, monkeypatch, {"session_id": "slash-preplan-1", "prompt": "/preplan prepare"})

    state, source = _planning(app_env.db_path, "slash-preplan-1")
    assert state == "preplanning"
    assert source == "preplan:prompt-submit"
    assert _subscription(app_env.db_path, "slash-preplan-1") == (
        "active",
        "preplan_plan",
        "/plan create the plan",
        1,
        "%54",
        "%54",
    )


def test_dollar_preplan_prompt_sets_preplanning_and_arms_plan_followup(app_env, monkeypatch):
    _insert_instance(app_env.db_path, "dollar-preplan-1", pane="%55", engine="codex")

    _prompt_submit(app_env, monkeypatch, {"session_id": "dollar-preplan-1", "prompt": "$preplan"})

    state, source = _planning(app_env.db_path, "dollar-preplan-1")
    assert state == "preplanning"
    assert source == "preplan:prompt-submit"
    assert _subscription(app_env.db_path, "dollar-preplan-1") == (
        "active",
        "preplan_plan",
        "/plan create the plan",
        1,
        "%55",
        "%55",
    )


def test_codex_prompt_submit_transcript_plan_context_sets_planning(
    app_env,
    monkeypatch,
    tmp_path,
):
    _insert_instance(app_env.db_path, "codex-plan-transcript-1", pane="%51", engine="codex")
    transcript = tmp_path / "codex-plan.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-plan-1","collaboration_mode_kind":"plan"}}',
                '{"type":"turn_context","payload":{"turn_id":"turn-plan-1","collaboration_mode":{"mode":"plan"}}}',
                '{"type":"event_msg","payload":{"type":"user_message","message":"native plan prompt without slash prefix"}}',
            ]
        ),
        encoding="utf-8",
    )

    _prompt_submit(
        app_env,
        monkeypatch,
        {
            "session_id": "codex-plan-transcript-1",
            "turn_id": "turn-plan-1",
            "transcript_path": str(transcript),
            "prompt": "native plan prompt without slash prefix",
        },
    )

    state, source = _planning(app_env.db_path, "codex-plan-transcript-1")
    assert state == "planning"
    assert source == "auto-clear:prompt-submit"


def test_codex_prompt_submit_ignores_prior_transcript_plan_turn(
    app_env,
    monkeypatch,
    tmp_path,
):
    _insert_instance(app_env.db_path, "codex-normal-transcript-1", pane="%52", engine="codex")
    transcript = tmp_path / "codex-normal.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"type":"event_msg","payload":{"type":"task_started","turn_id":"old-plan","collaboration_mode_kind":"plan"}}',
                '{"type":"turn_context","payload":{"turn_id":"old-plan","collaboration_mode":{"mode":"plan"}}}',
                '{"type":"event_msg","payload":{"type":"task_started","turn_id":"normal-turn","collaboration_mode_kind":"default"}}',
                '{"type":"turn_context","payload":{"turn_id":"normal-turn","collaboration_mode":{"mode":"default"}}}',
            ]
        ),
        encoding="utf-8",
    )

    _prompt_submit(
        app_env,
        monkeypatch,
        {
            "session_id": "codex-normal-transcript-1",
            "turn_id": "normal-turn",
            "transcript_path": str(transcript),
            "prompt": "ordinary prompt",
        },
    )

    state, source = _planning(app_env.db_path, "codex-normal-transcript-1")
    assert state == "none"
    assert source is None


def test_codex_prompt_submit_ignores_prior_turnless_plan_item(
    app_env,
    monkeypatch,
    tmp_path,
):
    _insert_instance(app_env.db_path, "codex-normal-transcript-2", pane="%53", engine="codex")
    transcript = tmp_path / "codex-turnless-old-plan.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"type":"event_msg","payload":{"type":"task_started","turn_id":"old-plan","collaboration_mode_kind":"plan"}}',
                '{"type":"event_msg","payload":{"type":"item_completed","item":{"type":"Plan","text":"old plan"}}}',
                '{"type":"event_msg","payload":{"type":"task_started","turn_id":"normal-turn","collaboration_mode_kind":"default"}}',
                '{"type":"turn_context","payload":{"turn_id":"normal-turn","collaboration_mode":{"mode":"default"}}}',
            ]
        ),
        encoding="utf-8",
    )

    _prompt_submit(
        app_env,
        monkeypatch,
        {
            "session_id": "codex-normal-transcript-2",
            "turn_id": "normal-turn",
            "transcript_path": str(transcript),
            "prompt": "ordinary prompt",
        },
    )

    state, source = _planning(app_env.db_path, "codex-normal-transcript-2")
    assert state == "none"
    assert source is None


# ── SessionStart reconciliation of a stuck row ─────────────────────────────────


def test_session_start_reregistration_reconciles_planning(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "stuck-1", pane="%47", planning_state="planning")

    async def no_label(_pane):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)

    async def run():
        return await hooks.handle_session_start(
            {
                "session_id": "stuck-1",
                "cwd": "/tmp",
                "env": {"TMUX_PANE": "%47", "TOKEN_API_ENGINE": "claude"},
            }
        )

    res = asyncio.run(run())
    assert res["action"] == "reregistered"
    state, source = _planning(app_env.db_path, "stuck-1")
    assert state == "none"
    assert source == "auto-clear:session-start"


def test_session_start_supplant_reconciles_planning_with_supplant_source(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(
        app_env.db_path,
        "old-plan-1",
        pane="%50",
        planning_state="approving",
        engine="codex",
        wrapper_launch_id="bridge-plan-1",
    )

    async def no_label(_pane):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)

    async def run():
        return await hooks.handle_session_start(
            {
                "session_id": "new-plan-1",
                "cwd": "/tmp",
                "env": {
                    "TMUX_PANE": "%50",
                    "TOKEN_API_ENGINE": "codex",
                    "TOKEN_API_WRAPPER_LAUNCH_ID": "bridge-plan-1",
                },
            }
        )

    res = asyncio.run(run())
    assert res["action"] == "supplanted"
    assert _planning(app_env.db_path, "old-plan-1") == (None, None)
    state, source = _planning(app_env.db_path, "new-plan-1")
    assert state == "none"
    assert source == "auto-clear:instance-supplanted"
