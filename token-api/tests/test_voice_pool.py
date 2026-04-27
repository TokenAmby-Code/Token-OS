"""Tests for voice pool assignment with linear probe.

Uses a temporary SQLite database via TOKEN_API_DB env var.
"""

import asyncio
import uuid

import pytest
import pytest_asyncio
import aiosqlite

PROFILES = []
FALLBACK_VOICES = []
ULTIMATE_FALLBACK = {}
get_next_available_profile = None
DB_PATH = None


@pytest.fixture(autouse=True)
def _bind_main_exports(app_env):
    global PROFILES, FALLBACK_VOICES, ULTIMATE_FALLBACK, get_next_available_profile, DB_PATH
    PROFILES = app_env.main.PROFILES
    FALLBACK_VOICES = app_env.main.FALLBACK_VOICES
    ULTIMATE_FALLBACK = app_env.main.ULTIMATE_FALLBACK
    get_next_available_profile = app_env.main.get_next_available_profile
    DB_PATH = app_env.main.DB_PATH


# ============ Unit tests for get_next_available_profile ============


class TestLinearProbe:
    """Test the linear probe voice assignment algorithm."""

    def test_empty_pool_assigns_from_primary(self):
        """First assignment should come from the primary (foreign) pool."""
        profile, exhausted = get_next_available_profile(set())
        assert profile in PROFILES
        assert not exhausted

    def test_no_duplicates_in_primary_pool(self):
        """Filling the primary pool should produce 9 unique voices."""
        used = set()
        for _ in range(len(PROFILES)):
            profile, exhausted = get_next_available_profile(used)
            assert profile["wsl_voice"] not in used, f"Duplicate: {profile['wsl_voice']}"
            assert not exhausted
            used.add(profile["wsl_voice"])

        assert len(used) == len(PROFILES)

    def test_fallback_after_primary_exhausted(self):
        """After 9 primary voices, should dip into fallback (David/Zira/Mark)."""
        used = {p["wsl_voice"] for p in PROFILES}

        profile, exhausted = get_next_available_profile(used)
        assert profile in FALLBACK_VOICES
        assert exhausted

    def test_fallback_voices_are_unique(self):
        """Fallback voices should also be assigned uniquely."""
        used = {p["wsl_voice"] for p in PROFILES}

        for _ in range(len(FALLBACK_VOICES)):
            profile, exhausted = get_next_available_profile(used)
            assert profile["wsl_voice"] not in used
            assert exhausted
            used.add(profile["wsl_voice"])

    def test_ultimate_fallback_when_all_exhausted(self):
        """When all 12 voices are taken, should return ultimate fallback (David)."""
        used = {p["wsl_voice"] for p in PROFILES}
        used |= {fb["wsl_voice"] for fb in FALLBACK_VOICES}

        profile, exhausted = get_next_available_profile(used)
        assert profile == ULTIMATE_FALLBACK
        assert exhausted
        assert profile["wsl_voice"] == "Microsoft David"

    def test_released_slot_is_reused(self):
        """Stopping an instance should free its voice for reassignment."""
        used = {p["wsl_voice"] for p in PROFILES}  # All 9 taken
        released_voice = PROFILES[3]["wsl_voice"]
        used.discard(released_voice)

        profile, exhausted = get_next_available_profile(used)
        assert profile["wsl_voice"] == released_voice
        assert not exhausted  # Back in primary pool

    def test_prefers_primary_over_fallback_on_release(self):
        """If both a primary and fallback slot are free, should pick primary."""
        # Take all primary + one fallback
        used = {p["wsl_voice"] for p in PROFILES}
        fb_profile, _ = get_next_available_profile(used)
        used.add(fb_profile["wsl_voice"])

        # Release one primary voice
        released = PROFILES[0]["wsl_voice"]
        used.discard(released)

        profile, exhausted = get_next_available_profile(used)
        assert profile["wsl_voice"] == released
        assert not exhausted  # Primary, not fallback

    def test_linear_probe_distribution(self):
        """Over many runs, all profiles should be assigned (not biased to one)."""
        counts = {p["wsl_voice"]: 0 for p in PROFILES}
        for _ in range(1000):
            profile, _ = get_next_available_profile(set())
            counts[profile["wsl_voice"]] += 1

        # Each voice should be picked at least once in 1000 runs (extremely high probability)
        for voice, count in counts.items():
            assert count > 0, f"{voice} was never assigned in 1000 runs"


# ============ Integration tests via API ============


class TestVoiceAssignmentAPI:
    """Test voice assignment through the full API registration flow."""

    @pytest.fixture
    def client(self, app_env, monkeypatch):
        """Create a test client for the FastAPI app."""
        async def _noop_push(*args, **kwargs):
            return None

        monkeypatch.setattr(app_env.main, "push_phone_widget_async", _noop_push)
        from fastapi.testclient import TestClient
        return TestClient(app_env.main.app)

    def _register(self, client, name: str) -> dict:
        """Helper to register an instance and return the response."""
        resp = client.post("/api/instances/register", json={
            "instance_id": str(uuid.uuid4()),
            "tab_name": name,
            "working_dir": f"/tmp/test-{name}",
        })
        assert resp.status_code == 200, f"Registration failed: {resp.text}"
        return resp.json()

    def test_primary_pool_assigns_unique_voices(self, client):
        """Registering one instance per primary voice should stay within the primary pool."""
        voices = set()
        for i in range(len(PROFILES)):
            data = self._register(client, f"inst-{i}")
            voice = data["profile"]["tts_voice"]
            assert voice not in voices, f"Duplicate voice: {voice}"
            voices.add(voice)

        primary_voices = {p["wsl_voice"] for p in PROFILES}
        assert voices == primary_voices

    def test_next_instance_after_primary_pool_gets_fallback(self, client):
        """The first registration after the primary pool is exhausted should use a fallback voice."""
        for i in range(len(PROFILES)):
            self._register(client, f"inst-{i}")

        data = self._register(client, "inst-fallback")
        voice = data["profile"]["tts_voice"]
        fallback_voices = {fb["wsl_voice"] for fb in FALLBACK_VOICES}
        assert voice in fallback_voices, f"Expected fallback, got: {voice}"

    def test_exhausted_pool_gets_ultimate_fallback(self, client):
        """After primary and fallback pools are full, the next registration uses the ultimate fallback."""
        total_before_ultimate = len(PROFILES) + len(FALLBACK_VOICES)
        for i in range(total_before_ultimate):
            self._register(client, f"inst-{i}")

        data = self._register(client, "inst-ultimate")
        voice = data["profile"]["tts_voice"]
        assert voice == "Microsoft David"

    def test_stopped_instance_releases_voice(self, client):
        """Stopping an instance should free its voice slot."""
        ids = []
        for i in range(len(PROFILES)):
            data = self._register(client, f"inst-{i}")
            ids.append(data)

        # All primary voices taken — next registration should get fallback
        data_10 = self._register(client, "inst-before-stop")
        fallback_voices = {fb["wsl_voice"] for fb in FALLBACK_VOICES}
        assert data_10["profile"]["tts_voice"] in fallback_voices

        # Stop one instance (the first one registered)
        first_id = ids[0]  # This is the profile response, need the instance_id
        # Use the API to stop — we need the actual instance_id from the DB
        resp = client.get("/api/instances")
        instances = resp.json()
        first_inst = [inst for inst in instances if inst["tab_name"] == "inst-0"][0]
        client.delete(f"/api/instances/{first_inst['id']}")

        # Now register again — should get a primary voice back
        data_11 = self._register(client, "inst-after-stop")
        primary_voices = {p["wsl_voice"] for p in PROFILES}
        assert data_11["profile"]["tts_voice"] in primary_voices

    def test_voice_pool_status_in_queue(self, client):
        """TTS queue status should include voice pool info."""
        resp = client.get("/api/notify/queue/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "voice_pool" in data
        assert data["voice_pool"]["total"] == len(PROFILES)
        assert data["voice_pool"]["fallback_count"] == len(FALLBACK_VOICES)
