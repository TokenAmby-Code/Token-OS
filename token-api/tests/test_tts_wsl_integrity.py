import hashlib
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


def test_speak_tts_wsl_rejects_success_with_mismatched_rendered_hash(monkeypatch):
    tts = _load_tts_routes()
    monkeypatch.setitem(tts.DESKTOP_CONFIG, "host", "wsl.local")
    monkeypatch.setitem(tts.DESKTOP_CONFIG, "port", 7777)

    def fake_post(*_args, **_kwargs):
        return FakeResponse(
            200,
            {
                "success": True,
                "method": "wsl_sapi",
                "rendered_hash": "0" * 64,
                "rendered_chars": 50,
            },
        )

    monkeypatch.setattr(tts.requests, "post", fake_post)

    result = tts.speak_tts_wsl("full message should not validate", "Microsoft David")

    assert result["success"] is False
    assert result["error"] == "satellite_text_integrity_check_failed"


def test_speak_tts_wsl_rejects_success_without_integrity_ack(monkeypatch):
    tts = _load_tts_routes()
    monkeypatch.setitem(tts.DESKTOP_CONFIG, "host", "wsl.local")
    monkeypatch.setitem(tts.DESKTOP_CONFIG, "port", 7777)

    def fake_post(*_args, **_kwargs):
        return FakeResponse(200, {"success": True, "method": "wsl_sapi"})

    monkeypatch.setattr(tts.requests, "post", fake_post)

    result = tts.speak_tts_wsl("legacy success without text ack", "Microsoft David")

    assert result["success"] is False
    assert result["error"] == "satellite_missing_text_integrity_ack"


def test_speak_tts_wsl_accepts_success_with_matching_rendered_hash(monkeypatch):
    tts = _load_tts_routes()
    message = "full message rendered through WSL SAPI file transport"
    rendered_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
    monkeypatch.setitem(tts.DESKTOP_CONFIG, "host", "wsl.local")
    monkeypatch.setitem(tts.DESKTOP_CONFIG, "port", 7777)

    def fake_post(*_args, **_kwargs):
        return FakeResponse(
            200,
            {
                "success": True,
                "method": "wsl_sapi",
                "transport": "wsl_sapi_text_file",
                "rendered_hash": rendered_hash,
                "rendered_chars": len(message),
            },
        )

    monkeypatch.setattr(tts.requests, "post", fake_post)

    result = tts.speak_tts_wsl(message, "Microsoft David")

    assert result["success"] is True
    assert result["transport"] == "wsl_sapi_text_file"
    assert result["message_chars"] == len(message)
    assert result["rendered_chars"] == len(message)
    assert result["rendered_hash"] == rendered_hash
