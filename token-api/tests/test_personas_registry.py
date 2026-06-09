import sqlite3
import uuid
from pathlib import Path

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
    assert fg["pane_tint"] == admin["pane_tint"] == "#300808"

    assert chapter["display_name"] == "Blood Angels"
    assert chapter["assignment_pool"] == "primary"
    assert chapter["tts_voice"] == "Microsoft Ravi"
    assert chapter["notification_sound"] == "notify.wav"


@pytest.mark.asyncio
async def test_assignment_exhausts_primary_before_backup(app_env):
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        first, exhausted = await personas.assign_astartes_persona(db, active_ids=set())
        assert first["slug"] == "blood-angels"
        assert exhausted is False

        primary_ids = {personas.persona_id_for_slug(seed.slug) for seed in personas.PRIMARY_ASTARTES}
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
            INSERT INTO claude_instances
              (id, session_id, origin_type, device_id, profile_name, status)
            VALUES ('i1', 's1', 'local', 'Mac-Mini', 'blood-angels', 'idle')
            """
        )
        await db.execute(
            """
            INSERT INTO claude_instances
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
    assert '"select-pane", "-t", tmux_pane, "-P", f"bg={bg}"' in shared


@pytest.mark.asyncio
async def test_null_tts_voice_queues_as_silent_not_fallback(app_env):
    from routes.tts import queue_tts

    async with aiosqlite.connect(app_env.db_path) as db:
        await db.execute(
            """
            INSERT INTO claude_instances
              (id, session_id, origin_type, device_id, profile_name, tts_voice, notification_sound, status)
            VALUES ('fg-silent', 'fg-silent-session', 'local', 'Mac-Mini', 'fabricator-general', NULL, NULL, 'idle')
            """
        )
        await db.commit()

    result = await queue_tts('fg-silent', 'Should not speak')
    assert result == {"success": True, "queued": False, "reason": "persona_silent"}


@pytest.mark.asyncio
async def test_future_instances_rank_retired_does_not_lock_persona(app_env):
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        await db.execute(
            """
            CREATE TABLE instances (
                id TEXT PRIMARY KEY,
                persona_id TEXT,
                rank TEXT,
                status TEXT
            )
            """
        )
        await db.execute(
            "INSERT INTO instances (id, persona_id, rank, status) VALUES ('retired', ?, 'retired', 'active')",
            (personas.persona_id_for_slug('blood-angels'),),
        )
        await db.execute(
            "INSERT INTO instances (id, persona_id, rank, status) VALUES ('active', ?, 'astartes', 'active')",
            (personas.persona_id_for_slug('ultramarines'),),
        )
        await db.commit()

        locked = await personas.active_non_retired_persona_ids(db)
        assert personas.persona_id_for_slug('blood-angels') not in locked
        assert personas.persona_id_for_slug('ultramarines') in locked
        assigned, _ = await personas.assign_astartes_persona(db)
        assert assigned['slug'] == 'blood-angels'
