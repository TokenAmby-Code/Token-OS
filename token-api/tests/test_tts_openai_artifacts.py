import importlib
import sys
from pathlib import Path


def _load_tts(tmp_path, monkeypatch):
    token_api_dir = Path(__file__).resolve().parents[1]
    if str(token_api_dir) not in sys.path:
        sys.path.insert(0, str(token_api_dir))
    monkeypatch.setenv("TOKEN_API_TTS_ARTIFACT_DIR", str(tmp_path))
    sys.modules.pop("routes.tts", None)
    return importlib.import_module("routes.tts")


def test_openai_tts_render_creates_wav_and_cache_hit(tmp_path, monkeypatch):
    tts = _load_tts(tmp_path, monkeypatch)
    calls = []

    class Resp:
        status_code = 200
        content = b"RIFFfake-wave"
        text = ""

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Resp()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(tts.requests, "post", fake_post)
    first = tts.render_openai_tts_artifact("hello", "ballad")
    second = tts.render_openai_tts_artifact("hello", "ballad")
    assert first["success"] is True
    assert second["success"] is True
    assert second["cache_hit"] is True
    assert len(calls) == 1
    assert Path(first["artifact_path"]).read_bytes() == b"RIFFfake-wave"


def test_artifact_url_is_never_loopback_when_mesh_ip_known(tmp_path, monkeypatch):
    tts = _load_tts(tmp_path, monkeypatch)

    monkeypatch.setenv("TOKEN_API_ADVERTISED_URL", "http://100.95.109.23:7777/")
    assert (
        tts._tts_artifact_public_url("a" * 32)
        == "http://100.95.109.23:7777/api/tts/artifacts/" + "a" * 32
    )

    monkeypatch.delenv("TOKEN_API_ADVERTISED_URL", raising=False)
    fake_cfg_module = type(sys)("imperium_config")
    fake_cfg_module.cfg = lambda key, machine=None: "100.1.2.3" if key == "tailscale_ip" else ""
    monkeypatch.setitem(sys.modules, "imperium_config", fake_cfg_module)
    assert tts._tts_artifact_base_url() == "http://100.1.2.3:7777"

    fake_cfg_module.cfg = lambda key, machine=None: ""
    assert tts._tts_artifact_base_url() == tts.TOKEN_API_URL


def test_openai_tts_render_failure_is_named(tmp_path, monkeypatch):
    tts = _load_tts(tmp_path, monkeypatch)
    monkeypatch.setattr(tts, "_openai_api_key", lambda: None)
    result = tts.render_openai_tts_artifact("hello", "ballad")
    assert result["success"] is False
    assert result["error"] == "openai_api_key_missing"
