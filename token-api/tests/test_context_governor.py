from __future__ import annotations

import asyncio
import sqlite3
import sys
from typing import Any


def _insert_instance(
    db_path,
    instance_id: str,
    *,
    engine: str = "claude",
    automated: int = 1,
    hook_driven: int = 0,
    is_subagent: int = 0,
    origin_type: str = "dispatch",
    commander_type: str = "emperor",
    commander_id: str | None = None,
    persona_slug: str | None = None,
    planning_state: str = "none",
    human_anchored_at: str | None = None,
    session_doc_id: int | None = 1,
):
    conn = sqlite3.connect(db_path)
    persona_id = None
    if persona_slug:
        row = conn.execute("SELECT id FROM personas WHERE slug = ?", (persona_slug,)).fetchone()
        if row is None:
            persona_id = f"persona-{persona_slug}"
            conn.execute(
                "INSERT INTO personas (id, slug, display_name, default_rank) VALUES (?, ?, ?, 'overseer')",
                (persona_id, persona_slug, persona_slug),
            )
        else:
            persona_id = row[0]
    conn.execute(
        """INSERT INTO instances
           (id, name, engine, working_dir, device_id, origin_type, commander_type, commander_id,
            status, persona_id, session_doc_id, automated, hook_driven, is_subagent,
            planning_state, human_anchored_at)
           VALUES (?, ?, ?, '/tmp', 'Mac-Mini', ?, ?, ?, 'working', ?, ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            instance_id,
            engine,
            origin_type,
            commander_type,
            commander_id,
            persona_id,
            session_doc_id,
            automated,
            hook_driven,
            is_subagent,
            planning_state,
            human_anchored_at,
        ),
    )
    conn.commit()
    conn.close()


def _patch_panes(monkeypatch, shared, mapping: dict[str, str]):
    async def resolve_instance_pane(instance_id):
        pane = mapping.get(instance_id)
        return (pane, pane) if pane else (None, None)

    async def instance_id_for_pane(pane):
        for inst, mapped in mapping.items():
            if mapped == pane:
                return inst
        return None

    monkeypatch.setattr(shared, "resolve_instance_pane", resolve_instance_pane)
    monkeypatch.setattr(shared, "instance_id_for_pane", instance_id_for_pane)


def test_soft_threshold_arms_one_shot_stop_subscription_without_clobbering_existing(
    app_env, monkeypatch
):
    cg = sys.modules["context_governor"]
    shared = sys.modules["shared"]
    _insert_instance(app_env.db_path, "worker-soft", automated=1, origin_type="dispatch")
    _patch_panes(monkeypatch, shared, {"worker-soft": "%101"})
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO stop_hook_subscriptions
           (target_instance_id, target_pane, subscriber_instance_id, subscriber_pane, purpose, payload)
           VALUES ('worker-soft', '%101', 'parent', '%999', 'generic', 'keep me')"""
    )
    conn.commit()
    conn.close()

    async def run():
        return await cg.ingest_context_telemetry(
            cg.ContextTelemetryRequest(
                instance_id="worker-soft",
                session_id="sess-soft",
                pane="%101",
                engine="claude",
                used_tokens=140_000,
                context_window_tokens=200_000,
                source="unit",
            )
        )

    result = asyncio.run(run())
    assert result["scoped"] is True
    assert result["stage"] == "soft_stop"
    assert result["action"] == "armed_stop_subscription"

    conn = sqlite3.connect(app_env.db_path)
    rows = conn.execute(
        "SELECT purpose, status, oneshot, target_instance_id, target_pane, subscriber_pane, payload "
        "FROM stop_hook_subscriptions ORDER BY id"
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    assert rows[0][0:3] == ("generic", "active", 0)
    context_row = rows[1]
    assert context_row[0:6] == ("context_governor_stop", "active", 1, "worker-soft", "%101", "%101")
    payload = context_row[6]
    assert "checkpoint your session doc" in payload
    assert payload.index("checkpoint your session doc") < payload.index("compact")


def test_hard_threshold_injects_once_and_debounces_storms(app_env, monkeypatch):
    cg = sys.modules["context_governor"]
    shared = sys.modules["shared"]
    _insert_instance(app_env.db_path, "worker-hard", automated=1, origin_type="dispatch")
    _patch_panes(monkeypatch, shared, {"worker-hard": "%102"})
    calls: list[dict[str, Any]] = []

    async def fake_actuate(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "result": {"status": "sent", "gated": False}}

    monkeypatch.setattr(cg, "_tmuxctld_context_governor_inject", fake_actuate)

    req = cg.ContextTelemetryRequest(
        instance_id="worker-hard",
        session_id="sess-hard",
        pane="%102",
        engine="claude",
        used_tokens=170_000,
        context_window_tokens=200_000,
        source="unit",
    )
    first = asyncio.run(cg.ingest_context_telemetry(req))
    second = asyncio.run(cg.ingest_context_telemetry(req))

    assert first["action"] == "forced_injection"
    assert second["action"] == "debounced"
    assert len(calls) == 1
    assert calls[0]["instance_id"] == "worker-hard"
    assert calls[0]["pane"] == "%102"
    assert "session doc" in calls[0]["text"]


def test_plan_mode_message_is_exact_and_never_mentions_compaction(app_env, monkeypatch):
    cg = sys.modules["context_governor"]
    shared = sys.modules["shared"]
    _insert_instance(
        app_env.db_path,
        "worker-plan",
        automated=1,
        origin_type="dispatch",
        planning_state="planning",
    )
    _patch_panes(monkeypatch, shared, {"worker-plan": "%103"})
    calls: list[dict[str, Any]] = []

    async def fake_actuate(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "result": {"status": "sent"}}

    monkeypatch.setattr(cg, "_tmuxctld_context_governor_inject", fake_actuate)

    result = asyncio.run(
        cg.ingest_context_telemetry(
            cg.ContextTelemetryRequest(
                instance_id="worker-plan",
                session_id="sess-plan",
                pane="%103",
                engine="claude",
                used_tokens=170_000,
                context_window_tokens=200_000,
            )
        )
    )

    assert result["action"] == "forced_injection"
    assert calls[0]["text"] == "Context full. Pose the plan without gathering context."
    assert "compact" not in calls[0]["text"].lower()
    assert "plan or" not in calls[0]["text"].lower()


def test_interactive_custodes_and_human_anchored_sessions_are_telemetry_only(app_env, monkeypatch):
    cg = sys.modules["context_governor"]
    shared = sys.modules["shared"]
    _insert_instance(
        app_env.db_path,
        "custodes-live",
        automated=0,
        hook_driven=0,
        origin_type="local",
        persona_slug="custodes",
    )
    _insert_instance(
        app_env.db_path,
        "human-anchored",
        automated=1,
        origin_type="dispatch",
        human_anchored_at="2026-07-03T12:00:00",
    )
    _patch_panes(monkeypatch, shared, {"custodes-live": "%104", "human-anchored": "%105"})

    async def boom(**kwargs):
        raise AssertionError("exempt sessions must not actuate")

    monkeypatch.setattr(cg, "_tmuxctld_context_governor_inject", boom)

    r1 = asyncio.run(
        cg.ingest_context_telemetry(
            cg.ContextTelemetryRequest(
                instance_id="custodes-live", pane="%104", used_tokens=200_000
            )
        )
    )
    r2 = asyncio.run(
        cg.ingest_context_telemetry(
            cg.ContextTelemetryRequest(
                instance_id="human-anchored", pane="%105", used_tokens=200_000
            )
        )
    )
    assert r1["scoped"] is False and r1["action"] == "telemetry_only"
    assert r2["scoped"] is False and r2["action"] == "telemetry_only"


def test_no_progress_sweep_marks_exhausted_and_routes_tmuxctld_stop(app_env, monkeypatch):
    cg = sys.modules["context_governor"]
    shared = sys.modules["shared"]
    _insert_instance(app_env.db_path, "worker-stale", automated=1, origin_type="dispatch")
    _patch_panes(monkeypatch, shared, {"worker-stale": "%106"})
    injections: list[dict[str, Any]] = []
    stops: list[dict[str, Any]] = []

    async def fake_inject(**kwargs):
        injections.append(kwargs)
        return {"ok": True, "result": {"status": "sent"}}

    async def fake_stop(**kwargs):
        stops.append(kwargs)
        return {"ok": True, "result": {"status": "stopped_autonomous_input"}}

    monkeypatch.setattr(cg, "_tmuxctld_context_governor_inject", fake_inject)
    monkeypatch.setattr(cg, "_tmuxctld_context_governor_stop", fake_stop)

    asyncio.run(
        cg.ingest_context_telemetry(
            cg.ContextTelemetryRequest(
                instance_id="worker-stale",
                session_id="sess-stale",
                pane="%106",
                used_tokens=170_000,
                no_progress_ttl_seconds=0,
            )
        )
    )
    sweep = asyncio.run(cg.sweep_context_governor())
    assert sweep["exhausted_count"] == 1
    assert stops and stops[0]["instance_id"] == "worker-stale"

    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute(
        "SELECT policy_state, stage FROM context_governor_state WHERE instance_id = 'worker-stale'"
    ).fetchone()
    conn.close()
    assert row == ("context_exhausted", "no_progress_stop")


def test_no_progress_sweep_skips_when_progress_was_observed(app_env, monkeypatch):
    cg = sys.modules["context_governor"]
    shared = sys.modules["shared"]
    _insert_instance(app_env.db_path, "worker-progress", automated=1, origin_type="dispatch")
    _patch_panes(monkeypatch, shared, {"worker-progress": "%107"})

    async def fake_inject(**kwargs):
        return {"ok": True, "result": {"status": "sent"}}

    async def boom_stop(**kwargs):
        raise AssertionError("observed progress must suppress no-progress hard stop")

    monkeypatch.setattr(cg, "_tmuxctld_context_governor_inject", fake_inject)
    monkeypatch.setattr(cg, "_tmuxctld_context_governor_stop", boom_stop)

    asyncio.run(
        cg.ingest_context_telemetry(
            cg.ContextTelemetryRequest(
                instance_id="worker-progress",
                session_id="sess-progress",
                pane="%107",
                used_tokens=170_000,
                no_progress_ttl_seconds=0,
            )
        )
    )
    asyncio.run(cg.record_context_governor_progress("worker-progress", "session_doc_checkpoint"))
    sweep = asyncio.run(cg.sweep_context_governor())
    assert sweep["exhausted_count"] == 0


def test_no_progress_sweep_ignores_historical_progress_before_injection(app_env, monkeypatch):
    cg = sys.modules["context_governor"]
    shared = sys.modules["shared"]
    _insert_instance(app_env.db_path, "worker-old-progress", automated=1, origin_type="dispatch")
    _patch_panes(monkeypatch, shared, {"worker-old-progress": "%108"})
    stops: list[dict[str, Any]] = []

    async def fake_inject(**kwargs):
        return {"ok": True, "result": {"status": "sent"}}

    async def fake_stop(**kwargs):
        stops.append(kwargs)
        return {"ok": True, "result": {"status": "stopped_autonomous_input"}}

    monkeypatch.setattr(cg, "_tmuxctld_context_governor_inject", fake_inject)
    monkeypatch.setattr(cg, "_tmuxctld_context_governor_stop", fake_stop)

    asyncio.run(cg.record_context_governor_progress("worker-old-progress", "previous_compaction"))
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        "UPDATE context_governor_state SET last_progress_at = '2000-01-01T00:00:00' "
        "WHERE instance_id = 'worker-old-progress'"
    )
    conn.commit()
    conn.close()
    asyncio.run(
        cg.ingest_context_telemetry(
            cg.ContextTelemetryRequest(
                instance_id="worker-old-progress",
                session_id="sess-old-progress",
                pane="%108",
                used_tokens=170_000,
                no_progress_ttl_seconds=0,
            )
        )
    )
    sweep = asyncio.run(cg.sweep_context_governor())
    assert sweep["exhausted_count"] == 1
    assert stops and stops[0]["instance_id"] == "worker-old-progress"
