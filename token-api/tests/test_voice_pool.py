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
        p["wsl_voice"] is None
        for p in app_env.shared.PERSONA_PROFILES
        if p["name"] != "custodes"
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
    rotation_voices = {p["wsl_voice"] for p in app_env.shared.PROFILES + app_env.shared.FALLBACK_VOICES}
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
