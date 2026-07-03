import sqlite3
import uuid
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_personas_seed_and_schema_constraints(app_env):
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        db.row_factory = aiosqlite.Row
        custodes = await personas.resolve_persona(db, "custodes")
        assert custodes["id"] == personas.persona_id_for_slug("custodes")
        assert custodes["tts_voice"] == "Microsoft George"
        assert custodes["pane_tint"] == "#302800"

    with sqlite3.connect(app_env.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO personas (id, slug, display_name, default_rank, assignment_pool)
                VALUES (?, 'bad-overseer', 'Bad Overseer', 'overseer', 'primary')
                """,
                (str(uuid.uuid4()),),
            )


@pytest.mark.asyncio
async def test_tts_policy_seeded_per_persona_deny_by_default(app_env: Any) -> None:
    """``personas.tts_policy`` is seeded with deny-by-default semantics.

    Custodes and Pax are ``hot`` council identities, voiced Astartes are
    ``pause``, and every other voiceless persona (FG, mechanicus,
    mechanicus-worker, primarchs, non-Pax civic seats) is ``silent``.
    """
    expected = {
        "custodes": "hot",
        "blood-angels": "pause",
        "ultramarines": "pause",
        "salamanders": "pause",
        "imperial-fists": "pause",
        "raven-guard": "pause",
        "space-wolves": "pause",
        "dark-angels": "pause",
        "white-scars": "pause",
        "deathwatch": "pause",
        "fabricator-general": "silent",
        "administratum": "silent",
        "inquisitor": "silent",
        "mechanicus": "silent",
        "mechanicus-worker": "silent",
        "pax": "hot",
        "orchestrator": "silent",
        "agentic-worker": "silent",
    }
    with sqlite3.connect(app_env.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = {
            row["slug"]: row["tts_policy"]
            for row in conn.execute("SELECT slug, tts_policy FROM personas").fetchall()
        }
    for slug, policy in expected.items():
        assert rows.get(slug) == policy, f"{slug} expected {policy}, got {rows.get(slug)!r}"
    # Deny-by-default: no seeded persona may carry a NULL/unknown policy.
    assert all(policy in ("silent", "hot", "pause") for policy in rows.values())


@pytest.mark.asyncio
async def test_pax_overseer_seed_resolves_silent_civic_seat(app_env):
    # Pax is the civic overseer seat on the council page: a non-40k civic overseer
    # singleton (the combined Custodes+Administratum interaction/record-keeper).
    # resolve_persona must surface it as an overseer (so the rank-stamp trigger
    # promotes its instance row off the astartes default) with the civic slate/
    # blue identity and no voice/sound (a voiceless seat).
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        pax = await personas.resolve_persona(db, "pax")

    assert pax is not None
    assert pax["id"] == personas.persona_id_for_slug("pax")
    assert pax["default_rank"] == "overseer"
    assert pax["pane_tint"] == "#1c2b3a"
    assert pax["chip_color"] == "#3a6ea5"
    assert pax["assignment_pool"] is None
    assert pax["assignment_order"] is None
    assert pax["tts_voice"] is None
    assert pax["notification_sound"] is None
    assert pax["silent"] is True


@pytest.mark.asyncio
async def test_civic_seats_resolve_after_seed(app_env):
    # The civic persona set seeds three personas: pax (overseer, above),
    # orchestrator (civic dispatch overseer) and agentic-worker (the civic worker
    # persona). All three must resolve after seeding, with civic-slate tints, and
    # all are silent (non-40k, no TTS). orchestrator is a singleton overseer;
    # agentic-worker is an astartes-rank worker kept OUT of the rotation pool so
    # it is resolved by slug in a civic worker context, never auto-assigned.
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        orchestrator = await personas.resolve_persona(db, "orchestrator")
        worker = await personas.resolve_persona(db, "agentic-worker")

    assert orchestrator is not None
    assert orchestrator["id"] == personas.persona_id_for_slug("orchestrator")
    assert orchestrator["default_rank"] == "overseer"
    assert orchestrator["pane_tint"] == "#14302a"
    assert orchestrator["assignment_pool"] is None
    assert orchestrator["silent"] is True

    assert worker is not None
    assert worker["id"] == personas.persona_id_for_slug("agentic-worker")
    assert worker["default_rank"] == "astartes"
    assert worker["pane_tint"] == "#23323f"
    # Out of the auto-assignment rotation: no pool/order means it is never handed
    # out by assign_astartes_persona.
    assert worker["assignment_pool"] is None
    assert worker["assignment_order"] is None
    assert worker["silent"] is True


@pytest.mark.asyncio
async def test_resolver_silent_and_voiced_personas(app_env):
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        custodes = await personas.resolve_persona(db, "custodes")
        fg = await personas.resolve_persona(db, "fabricator-general")
        admin = await personas.resolve_persona(db, "administratum")
        chapter = await personas.resolve_persona(db, "blood-angels")

    assert custodes["tts_voice"] == "Microsoft George"
    assert custodes["notification_sound"] == "chimes.wav"
    assert custodes["silent"] is False

    assert fg["tts_voice"] is None and fg["silent"] is True
    assert admin["tts_voice"] is None and admin["silent"] is True
    assert fg["pane_tint"] == "#300808"
    assert admin["pane_tint"] == "#24201a"

    # The Custodes Trinity shares the daily-note-as-session-doc default; workers do not.
    assert custodes["default_session_doc"] == "daily_note"
    assert fg["default_session_doc"] == "daily_note"
    assert admin["default_session_doc"] == "daily_note"
    assert chapter["default_session_doc"] is None

    assert chapter["display_name"] == "Blood Angels"
    assert chapter["assignment_pool"] == "primary"
    assert chapter["tts_voice"] == "Microsoft Ravi"
    assert chapter["chip_color"] == "#b1191e"
    assert chapter["pane_tint"] == "#2a1020"
    assert chapter["notification_sound"] == "notify.wav"


def test_overseer_tints_do_not_collide_with_worker_or_primarch_tints(app_env) -> None:
    """Pane backgrounds are an operator-recognition channel.

    Worker tints include chapter personas plus shared worker coats such as
    mechanicus-worker. Nothing outside the overseer tier may reuse an overseer
    pane tint; exact collisions made Blood Angels / Mechanicus workers look like
    the Fabricator-General and Imperial Fists look like Custodes.
    """
    with sqlite3.connect(app_env.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT slug, default_rank, pane_tint
            FROM personas
            WHERE pane_tint IS NOT NULL AND pane_tint <> 'default'
            """
        ).fetchall()

    overseer_by_tint = {
        row["pane_tint"]: row["slug"] for row in rows if row["default_rank"] == "overseer"
    }
    non_overseer_collisions = [
        (row["slug"], row["default_rank"], row["pane_tint"], overseer_by_tint[row["pane_tint"]])
        for row in rows
        if row["default_rank"] != "overseer" and row["pane_tint"] in overseer_by_tint
    ]
    assert non_overseer_collisions == []

    overseer_counts: dict[str, list[str]] = {}
    for row in rows:
        if row["default_rank"] == "overseer":
            overseer_counts.setdefault(row["pane_tint"], []).append(row["slug"])
    duplicate_overseers = {tint: slugs for tint, slugs in overseer_counts.items() if len(slugs) > 1}
    assert duplicate_overseers == {}


@pytest.mark.asyncio
async def test_group_a_astartes_seed_preferred_voices_and_dark_tints(app_env):
    import personas

    expected = {
        "blood-angels": ("Microsoft Ravi", "#b1191e", "#2a1020"),
        "ultramarines": ("Microsoft Susan", "#1f4e9b", "#081c30"),
        "salamanders": ("Microsoft Sean", "#1b7a3d", "#082810"),
        "imperial-fists": ("Microsoft Catherine", "#e6b800", "#3a3000"),
        "raven-guard": ("Microsoft Heera", "#2b2b2b", "#101010"),
    }
    async with aiosqlite.connect(app_env.db_path) as db:
        for slug, (voice, chip, tint) in expected.items():
            row = await personas.resolve_persona(db, slug)
            assert row["tts_voice"] == voice
            assert row["chip_color"] == chip
            assert row["pane_tint"] == tint


@pytest.mark.asyncio
async def test_assignment_exhausts_primary_before_backup(app_env):
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        first, exhausted = await personas.assign_astartes_persona(db, active_ids=set())
        assert first["slug"] == "blood-angels"
        assert exhausted is False

        primary_ids = {
            personas.persona_id_for_slug(seed.slug) for seed in personas.PRIMARY_ASTARTES
        }
        backup, exhausted = await personas.assign_astartes_persona(db, active_ids=primary_ids)
        assert backup["slug"] == "space-wolves"
        assert backup["assignment_pool"] == "backup"
        assert exhausted is True


@pytest.mark.asyncio
async def test_active_legacy_instances_lock_personas_but_stopped_release(app_env):
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        await db.execute(
            """
            INSERT INTO legacy_instances
              (id, session_id, origin_type, device_id, profile_name, status)
            VALUES ('i1', 's1', 'local', 'Mac-Mini', 'blood-angels', 'idle')
            """
        )
        await db.execute(
            """
            INSERT INTO legacy_instances
              (id, session_id, origin_type, device_id, profile_name, status)
            VALUES ('i2', 's2', 'local', 'Mac-Mini', 'ultramarines', 'stopped')
            """
        )
        await db.commit()

        locked = await personas.active_non_retired_persona_ids(db)
        assert personas.persona_id_for_slug("blood-angels") in locked
        assert personas.persona_id_for_slug("ultramarines") not in locked

        assigned, _ = await personas.assign_astartes_persona(db)
        assert assigned["slug"] == "ultramarines"


def test_register_api_returns_persona_display_and_no_cc_color(app_env, monkeypatch):
    async def _noop_push(*args, **kwargs):
        return None

    monkeypatch.setattr(app_env.main, "push_phone_widget_async", _noop_push)
    client = TestClient(app_env.main.app)
    resp = client.post(
        "/api/instances/register",
        json={"instance_id": str(uuid.uuid4()), "tab_name": "inst", "working_dir": "/tmp/inst"},
    )
    assert resp.status_code == 200, resp.text
    profile = resp.json()["profile"]
    assert profile["name"] == "blood-angels"
    assert profile["tts_voice"] == "Microsoft Ravi"
    assert profile["chip_color"] == "#b1191e"
    assert "cc_color" not in profile


def test_recoloring_has_no_slash_color_path():
    root = Path(__file__).resolve().parents[2]
    checked = [
        root / "token-api" / "routes" / "hooks.py",
        root / "token-api" / "main.py",
        root / "claude-config" / "hooks" / "generic-hook.sh",
        root / "cli-tools" / "bin" / "instance-name",
        root / "cli-tools" / "bin" / "pending-ui-flush",
    ]
    for path in checked:
        assert "/color" not in path.read_text(encoding="utf-8")

    shared = (root / "token-api" / "shared.py").read_text(encoding="utf-8")
    assert "adapter.set_pane_tint(tmux_pane, bg)" in shared
    assert 'select-pane", "-t", tmux_pane, "-P"' not in shared
    assert "LEGION_PANE_COLORS" not in shared


@pytest.mark.asyncio
async def test_null_tts_voice_queues_as_silent_not_fallback(app_env):
    from routes.tts import queue_tts

    async with aiosqlite.connect(app_env.db_path) as db:
        await db.execute(
            """
            INSERT INTO legacy_instances
              (id, session_id, origin_type, device_id, profile_name, tts_voice, notification_sound, status)
            VALUES ('fg-silent', 'fg-silent-session', 'local', 'Mac-Mini', 'fabricator-general', NULL, NULL, 'idle')
            """
        )
        await db.commit()

    result = await queue_tts("fg-silent", "Should not speak")
    assert result == {"success": True, "queued": False, "reason": "persona_silent"}


@pytest.mark.asyncio
async def test_future_instances_rank_retired_does_not_lock_persona(app_env):
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        await db.execute(
            """INSERT INTO instances
               (id, name, device_id, commander_type, status, persona_id, rank)
               VALUES ('retired', 'retired', 'Mac-Mini', 'emperor', 'idle', ?, 'retired')""",
            (personas.persona_id_for_slug("blood-angels"),),
        )
        await db.execute(
            """INSERT INTO instances
               (id, name, device_id, commander_type, status, persona_id, rank)
               VALUES ('active', 'active', 'Mac-Mini', 'emperor', 'idle', ?, 'astartes')""",
            (personas.persona_id_for_slug("ultramarines"),),
        )
        await db.commit()

        locked = await personas.active_non_retired_persona_ids(db)
        assert personas.persona_id_for_slug("blood-angels") not in locked
        assert personas.persona_id_for_slug("ultramarines") in locked
        assigned, _ = await personas.assign_astartes_persona(db)
        assert assigned["slug"] == "blood-angels"


@pytest.mark.asyncio
async def test_persona_tint_for_instance_uses_canonical_persona_id(app_env):
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        await db.execute(
            """INSERT INTO instances
               (id, name, device_id, commander_type, status, persona_id, rank)
               VALUES ('chapter', 'chapter', 'Mac-Mini', 'emperor', 'idle', ?, 'astartes')""",
            (personas.persona_id_for_slug("ultramarines"),),
        )
        await db.execute(
            """INSERT INTO instances
               (id, name, device_id, commander_type, status, persona_id, rank)
               VALUES ('civic', 'civic', 'Mac-Mini', 'emperor', 'idle', NULL, 'astartes')"""
        )
        await db.execute(
            """INSERT INTO instances
               (id, name, device_id, commander_type, status, persona_id, rank)
               VALUES ('custodes', 'custodes', 'Mac-Mini', 'emperor', 'idle', ?, 'overseer')""",
            (personas.persona_id_for_slug("custodes"),),
        )
        await db.execute(
            """INSERT INTO instances
               (id, name, device_id, commander_type, status, persona_id, rank)
               VALUES ('fg', 'fg', 'Mac-Mini', 'emperor', 'idle', ?, 'overseer')""",
            (personas.persona_id_for_slug("fabricator-general"),),
        )
        await db.commit()

        assert await personas.persona_tint_for_instance(db, "chapter") == "#081c30"
        assert await personas.persona_tint_for_instance(db, "civic") == "default"
        assert await personas.persona_tint_for_instance(db, "custodes") == "#302800"
        assert await personas.persona_tint_for_instance(db, "fg") == "#300808"


@pytest.mark.asyncio
async def test_repair_legacy_active_persona_assignments(app_env):
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        await db.execute(
            """
            INSERT INTO legacy_instances
              (id, session_id, origin_type, device_id, legion, primarch, profile_name,
               tts_voice, notification_sound, status)
            VALUES
              ('legacy-custodes', 'legacy-custodes-s', 'local', 'Mac-Mini',
               'custodes', 'custodes', NULL, NULL, NULL, 'idle'),
              ('legacy-fg', 'legacy-fg-s', 'local', 'Mac-Mini',
               'fabricator', 'fabricator-general', 'profile_1', 'Microsoft George', 'chimes.wav', 'idle'),
              ('legacy-worker', 'legacy-worker-s', 'local', 'Mac-Mini',
               'astartes', NULL, 'emperors-children', 'Microsoft Heera', 'chimes.wav', 'idle')
            """
        )
        await db.commit()

        repaired = await personas.repair_legacy_instance_personas(db)
        await db.commit()
        assert repaired == 1

        cursor = await db.execute(
            """
            SELECT id, profile_name, tts_voice, notification_sound
            FROM legacy_instances
            WHERE id IN ('legacy-custodes', 'legacy-fg', 'legacy-worker')
            ORDER BY id
            """
        )
        rows = await cursor.fetchall()

    assert rows == [
        ("legacy-custodes", "custodes", "Microsoft George", "chimes.wav"),
        ("legacy-fg", "fabricator-general", None, None),
        ("legacy-worker", "blood-angels", "Microsoft Ravi", "notify.wav"),
    ]
