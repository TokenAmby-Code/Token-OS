import hashlib
import importlib.util
from pathlib import Path


def _load_satellite_module():
    path = Path(__file__).resolve().parents[1] / "token-satellite.py"
    spec = importlib.util.spec_from_file_location("token_satellite_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_satellite_tmux_send_payload_then_submit_separates_enter(monkeypatch):
    satellite = _load_satellite_module()
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(satellite.subprocess, "run", fake_run)

    satellite._tmux_send_payload_then_submit("%10", "resume work", clear_prompt=True)

    assert [call[0] for call in calls] == [
        ["tmux", "send-keys", "-t", "%10", "C-u"],
        ["tmux", "send-keys", "-t", "%10", "-l", "resume work"],
        ["tmux", "send-keys", "-t", "%10", "C-m"],
    ]


def test_satellite_tmux_send_payload_tabs_codex_skill_before_enter(monkeypatch):
    satellite = _load_satellite_module()
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(satellite.subprocess, "run", fake_run)

    satellite._tmux_send_payload_then_submit(
        "%10",
        '$golden-throne-sop victory condition "needs tests passing" is unmet',
        clear_prompt=True,
        enable_skill_sink=True,
    )

    assert [call[0] for call in calls] == [
        ["tmux", "send-keys", "-t", "%10", "C-u"],
        [
            "tmux",
            "send-keys",
            "-t",
            "%10",
            "-l",
            '$golden-throne-sop victory condition "needs tests passing" is unmet',
        ],
        ["tmux", "send-keys", "-t", "%10", "Tab"],
        ["tmux", "send-keys", "-t", "%10", "C-m"],
        ["tmux", "send-keys", "-t", "%10", "C-m"],
    ]


def test_satellite_tmux_send_payload_does_not_tab_claude_skill(monkeypatch):
    satellite = _load_satellite_module()
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(satellite.subprocess, "run", fake_run)

    satellite._tmux_send_payload_then_submit("%10", "/golden-throne-sop needs x", clear_prompt=True)

    assert [call[0] for call in calls] == [
        ["tmux", "send-keys", "-t", "%10", "C-u"],
        ["tmux", "send-keys", "-t", "%10", "-l", "/golden-throne-sop needs x"],
        ["tmux", "send-keys", "-t", "%10", "C-m"],
    ]


def test_satellite_golden_throne_live_agent_uses_file_for_multiline_prompt(monkeypatch, tmp_path):
    satellite = _load_satellite_module()
    sent = []
    written = {}

    class FakePath:
        def __init__(self, path):
            self.path = path

        def write_text(self, text):
            written[self.path] = text

    monkeypatch.setattr(satellite, "Path", lambda path: FakePath(path))
    monkeypatch.setattr(satellite, "_tmux_pane_has_pending_input", lambda pane: False)
    monkeypatch.setattr(satellite, "_tmux_pane_has_agent_process", lambda pane, engine: True)
    monkeypatch.setattr(
        satellite.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": "codex\n"})(),
    )
    monkeypatch.setattr(
        satellite,
        "_tmux_send_payload_then_submit",
        lambda pane, payload, clear_prompt=False, enable_skill_sink=False: sent.append(
            (pane, payload, clear_prompt, enable_skill_sink)
        ),
    )

    import asyncio

    req = satellite.GoldenThroneFollowupRequest(
        session_id="abcdef1234567890",
        tmux_pane="%10",
        working_dir=str(tmp_path),
        prompt="line one\nline two",
        prompt_summary="GT kreig north needs tests passing",
        engine="codex",
    )
    result = asyncio.run(satellite.golden_throne_followup(req))

    assert result["success"] is True
    assert sent == [
        (
            "%10",
            "GT kreig north needs tests passing. Run: cat /tmp/golden-throne-sop-abcdef12.md, then address those criteria.",
            True,
            True,
        )
    ]
    assert written["/tmp/golden-throne-sop-abcdef12.md"] == "line one\nline two"


def test_tts_engine_speak_uses_text_file_transport_and_hash_ack(monkeypatch, tmp_path):
    satellite = _load_satellite_module()
    engine = satellite.TTSEngine()
    message = "alpha " * 52
    sent = []
    responses = iter(
        [
            f"OK:{len(message)}:" + hashlib.sha256(message.encode("utf-8")).hexdigest(),
            "Ready",
        ]
    )

    monkeypatch.setattr(engine, "TTS_DIR_WSL", str(tmp_path))
    monkeypatch.setattr(engine, "TTS_DIR_WIN", r"C:\temp\tts")
    monkeypatch.setattr(engine, "_ensure_running", lambda: None)
    monkeypatch.setattr(engine, "_send", lambda cmd: sent.append(cmd))
    monkeypatch.setattr(engine, "_readline", lambda: next(responses))
    monkeypatch.setattr(satellite.time, "sleep", lambda _seconds: None)

    result = engine.speak(message, "Microsoft David", 0)

    assert result["success"] is True
    assert result["transport"] == "wsl_sapi_text_file"
    assert result["message_chars"] == len(message)
    assert sent[0]["action"] == "speak"
    assert "message" not in sent[0]
    assert sent[0]["message_file"].endswith(".txt")
    text_file = next(tmp_path.glob("*.txt"))
    assert text_file.read_text(encoding="utf-8") == message
    assert sent[1] == {"action": "poll"}


def test_tts_engine_speak_fails_on_text_integrity_mismatch(monkeypatch, tmp_path):
    satellite = _load_satellite_module()
    engine = satellite.TTSEngine()

    monkeypatch.setattr(engine, "TTS_DIR_WSL", str(tmp_path))
    monkeypatch.setattr(engine, "TTS_DIR_WIN", r"C:\temp\tts")
    monkeypatch.setattr(engine, "_ensure_running", lambda: None)
    monkeypatch.setattr(engine, "_send", lambda _cmd: None)
    monkeypatch.setattr(engine, "_readline", lambda: "OK:50:" + ("0" * 64))

    result = engine.speak("this text should not be acknowledged as rendered", "Microsoft David", 0)

    assert result["success"] is False
    assert result["error"] == "TTS text integrity check failed"


def test_tts_engine_synthesize_uses_text_file_transport(monkeypatch, tmp_path):
    satellite = _load_satellite_module()
    engine = satellite.TTSEngine()
    sent = []
    message = "beta " * 60
    response = "SYNTH_OK:300:" + hashlib.sha256(message.encode("utf-8")).hexdigest()

    monkeypatch.setattr(engine, "TTS_DIR_WSL", str(tmp_path))
    monkeypatch.setattr(engine, "TTS_DIR_WIN", r"C:\temp\tts")
    monkeypatch.setattr(engine, "_ensure_running", lambda: None)
    monkeypatch.setattr(engine, "_send", lambda cmd: sent.append(cmd))
    monkeypatch.setattr(engine, "_readline", lambda: response)

    result = engine.synthesize(message, "Microsoft David", 0, file_id="fixed-id")

    assert result["success"] is True
    assert result["transport"] == "wsl_sapi_text_file"
    assert result["file_id"] == "fixed-id"
    assert result["message_chars"] == len(message)
    assert sent == [
        {
            "action": "synthesize",
            "voice": "Microsoft David",
            "rate": 0,
            "message_file": r"C:\temp\tts\fixed-id.txt",
            "file_id": "fixed-id",
        }
    ]
    assert (tmp_path / "fixed-id.txt").read_text(encoding="utf-8") == message
