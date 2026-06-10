"""Compatibility tests for the persona-backed voice/profile projection."""

import uuid

import aiosqlite
import pytest
from fastapi.testclient import TestClient


def test_compat_profiles_have_no_cc_color(app_env):
    all_slots = [
        *app_env.shared.PROFILES,
        *app_env.shared.FALLBACK_VOICES,
        app_env.shared.ULTIMATE_FALLBACK,
        *app_env.shared.PERSONA_PROFILES,
    ]
    assert all("cc_color" not in slot for slot in all_slots)
    assert app_env.shared.CUSTODES_PROFILE["wsl_voice"] == "Microsoft George"
    assert all(
        p["wsl_voice"] is None for p in app_env.shared.PERSONA_PROFILES if p["name"] != "custodes"
    )


def test_legacy_profile_by_name_resolves_persona_projection(app_env):
    blood = app_env.shared.profile_by_name("blood-angels")
    assert blood["chapter"] == "Blood Angels"
    assert blood["chip_color"] == "#b1191e"
    assert blood["pane_tint"] == "default"
    assert app_env.shared.profile_by_name("profile_3") is None


@pytest.mark.asyncio
async def test_persona_assignment_primary_then_backup(app_env):
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        first, exhausted = await personas.assign_astartes_persona(db, active_ids=set())
        assert first["slug"] == app_env.shared.PROFILES[0]["name"]
        assert exhausted is False

        primary_ids = {personas.persona_id_for_slug(p["name"]) for p in app_env.shared.PROFILES}
        backup, exhausted = await personas.assign_astartes_persona(db, active_ids=primary_ids)
        assert backup["slug"] == app_env.shared.FALLBACK_VOICES[0]["name"]
        assert exhausted is True


def test_custodes_voice_reserved_from_astartes_assignment(app_env):
    rotation_voices = {
        p["wsl_voice"] for p in app_env.shared.PROFILES + app_env.shared.FALLBACK_VOICES
    }
    rotation_voices.add(app_env.shared.ULTIMATE_FALLBACK["wsl_voice"])
    assert app_env.shared.CUSTODES_PROFILE["wsl_voice"] not in rotation_voices


@pytest.fixture
def client(app_env, monkeypatch):
    async def _noop_push(*args, **kwargs):
        return None

    monkeypatch.setattr(app_env.main, "push_phone_widget_async", _noop_push)
    return TestClient(app_env.main.app)


def _register(client, name: str) -> dict:
    resp = client.post(
        "/api/instances/register",
        json={"instance_id": str(uuid.uuid4()), "tab_name": name, "working_dir": f"/tmp/{name}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_registration_exhausts_primary_before_backup(client, app_env):
    names = []
    for i in range(len(app_env.shared.PROFILES)):
        names.append(_register(client, f"inst-{i}")["profile"]["name"])
    assert names == [p["name"] for p in app_env.shared.PROFILES]

    fallback = _register(client, "inst-fallback")["profile"]
    assert fallback["name"] == app_env.shared.FALLBACK_VOICES[0]["name"]
    assert "cc_color" not in fallback


def test_stopped_instance_releases_persona(client):
    first = _register(client, "inst-0")
    assert first["profile"]["name"] == "blood-angels"

    instances = client.get("/api/instances").json()
    inst = [row for row in instances if row["tab_name"] == "inst-0"][0]
    client.delete(f"/api/instances/{inst['id']}")

    again = _register(client, "inst-1")
    assert again["profile"]["name"] == "blood-angels"


def test_voice_pool_status_in_queue(client, app_env):
    resp = client.get("/api/notify/queue/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["voice_pool"]["total"] == len(app_env.shared.PROFILES)
    assert data["voice_pool"]["fallback_count"] == len(app_env.shared.FALLBACK_VOICES)


def test_manual_voice_change_updates_persona_and_tts_fields(client, app_env):
    first = _register(client, "inst-0")
    second = _register(client, "inst-1")
    assert first["profile"]["name"] == "blood-angels"
    assert second["profile"]["name"] == "ultramarines"

    instances = client.get("/api/instances").json()
    second_row = [row for row in instances if row["tab_name"] == "inst-1"][0]
    resp = client.patch(
        f"/api/instances/{second_row['id']}/voice",
        json={"voice": "Microsoft Catherine"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "voice_changed"
    assert data["profile_name"] == "imperial-fists"
    assert data["profile"]["notification_sound"] == "ding.wav"

    import sqlite3

    with sqlite3.connect(app_env.db_path) as conn:
        row = conn.execute(
            "SELECT profile_name, tts_voice, notification_sound FROM claude_instances WHERE id = ?",
            (second_row["id"],),
        ).fetchone()
    assert row == ("imperial-fists", "Microsoft Catherine", "ding.wav")


def test_manual_voice_change_rejects_collision_without_bumping(client, app_env):
    first = _register(client, "inst-0")
    second = _register(client, "inst-1")
    assert first["profile"]["tts_voice"] == "Microsoft Ravi"
    assert second["profile"]["tts_voice"] == "Microsoft Susan"

    instances = client.get("/api/instances").json()
    second_row = [row for row in instances if row["tab_name"] == "inst-1"][0]
    resp = client.patch(
        f"/api/instances/{second_row['id']}/voice",
        json={"voice": "Microsoft Ravi"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "voice_in_use"

    import sqlite3

    with sqlite3.connect(app_env.db_path) as conn:
        rows = conn.execute(
            "SELECT tab_name, profile_name, tts_voice FROM claude_instances ORDER BY tab_name"
        ).fetchall()
    assert rows == [
        ("inst-0", "blood-angels", "Microsoft Ravi"),
        ("inst-1", "ultramarines", "Microsoft Susan"),
    ]


def test_manual_voice_change_rejects_reserved_custodes_voice(client):
    row = _register(client, "inst-0")
    instances = client.get("/api/instances").json()
    inst = [item for item in instances if item["tab_name"] == "inst-0"][0]
    assert row["profile"]["name"] == "blood-angels"

    resp = client.patch(
        f"/api/instances/{inst['id']}/voice",
        json={"voice": "Microsoft George"},
    )
    assert resp.status_code == 400, resp.text
    assert "Invalid Astartes persona voice" in resp.json()["detail"]
