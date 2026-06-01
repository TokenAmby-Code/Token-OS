"""Anti-blind invariant for the Discord-voice TTS path.

Regression guard for `feedback_anti_blind_dedup`: TTS must not claim success
unless a live voice consumer actually received (or is bounded-window queued for)
the audio. The daemon historically reported a destroyed connection as
`connected` and resolved playback as `played:true` against a dead pipe; these
tests pin the Token-API-side defenses so that lie can no longer surface as a
success.

These are unit additions, not a substitute for the live-path validation the
routing fix was exercised against.
"""

import importlib
import sys
from pathlib import Path


def _load_tts_routes():
    token_api_dir = Path(__file__).resolve().parents[1]
    if str(token_api_dir) not in sys.path:
        sys.path.insert(0, str(token_api_dir))
    return importlib.import_module("routes.tts")


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _reset_bot_cache(tts):
    # _get_discord_voice_bot caches for 5s on the function object.
    for attr in ("_result", "_checked"):
        if hasattr(tts._get_discord_voice_bot, attr):
            delattr(tts._get_discord_voice_bot, attr)


def test_get_discord_voice_bot_requires_ready_connection(monkeypatch):
    """A bot reported `connected` but not `ready` (stale/half-open connection)
    must not be selected as a voice consumer."""
    tts = _load_tts_routes()
    _reset_bot_cache(tts)

    def fake_get(*_args, **_kwargs):
        return FakeResponse(
            200,
            {
                "custodes": {
                    "connected": True,
                    "connectionState": "connecting",  # not yet usable
                    "channelId": "123",
                },
            },
        )

    monkeypatch.setattr(tts.requests, "get", fake_get)
    assert tts._get_discord_voice_bot() is None


def test_get_discord_voice_bot_accepts_ready_connection(monkeypatch):
    tts = _load_tts_routes()
    _reset_bot_cache(tts)

    def fake_get(*_args, **_kwargs):
        return FakeResponse(
            200,
            {
                "custodes": {
                    "connected": True,
                    "connectionState": "ready",
                    "channelId": "123",
                },
            },
        )

    monkeypatch.setattr(tts.requests, "get", fake_get)
    assert tts._get_discord_voice_bot() == "custodes"


def test_speak_tts_discord_no_success_without_played(monkeypatch):
    """Daemon accepts the request (200) but does not confirm playback: the
    routing core must report failure with an actionable reason, not success."""
    tts = _load_tts_routes()

    def fake_post(*_args, **_kwargs):
        return FakeResponse(200, {"played": False})

    monkeypatch.setattr(tts.requests, "post", fake_post)

    result = tts.speak_tts_discord("audio into the void", "custodes")
    assert result["success"] is False
    assert result["reason"] == "bot_not_in_channel"


def test_speak_tts_discord_success_when_played(monkeypatch):
    tts = _load_tts_routes()

    def fake_post(*_args, **_kwargs):
        return FakeResponse(200, {"played": True})

    monkeypatch.setattr(tts.requests, "post", fake_post)

    result = tts.speak_tts_discord("audio that lands", "custodes")
    assert result["success"] is True
    assert result["method"] == "discord_voice"
    assert result["bot"] == "custodes"
