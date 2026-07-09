import hashlib
import importlib.util
from pathlib import Path

import pytest


def _load_satellite_module():
    path = Path(__file__).resolve().parents[1] / "token-satellite.py"
    spec = importlib.util.spec_from_file_location("token_satellite_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_satellite_health_reports_runtime_git_sha(monkeypatch):
    satellite = _load_satellite_module()
    monkeypatch.setattr(satellite, "_runtime_git_sha", lambda: "abc123")

    import asyncio

    payload = asyncio.run(satellite.health())
    assert payload["git_sha"] == "abc123"
    assert payload["runtime_path"] == str(satellite.REPO_ROOT)


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


def test_ahk_scripts_dir_env_override_blocks_path_escape(monkeypatch, tmp_path):
    ahk_dir = tmp_path / "ahk-cache"
    ahk_dir.mkdir()
    (ahk_dir / "ok.ahk").write_text("MsgBox 'ok'\n", encoding="utf-8")
    outside = tmp_path / "evil.ahk"
    outside.write_text("bad\n", encoding="utf-8")
    monkeypatch.setenv("TOKEN_OS_AHK_DIR", str(ahk_dir))

    satellite = _load_satellite_module()
    assert satellite.AHK_SCRIPTS_DIR == ahk_dir

    import asyncio

    req = satellite.AhkRequest(script="../evil.ahk")
    try:
        asyncio.run(satellite.execute_ahk(req))
    except Exception as exc:
        assert getattr(exc, "status_code", None) in {403, 404}
    else:
        raise AssertionError("path escape unexpectedly allowed")


def test_runtime_refresh_rejects_missing_and_bad_bearer(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_SATELLITE_REFRESH_SECRET", "refresh-secret")
    helper = tmp_path / "token-satellite-refresh"
    helper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setenv("TOKEN_SATELLITE_REFRESH_HELPER", str(helper))

    satellite = _load_satellite_module()
    from fastapi.testclient import TestClient

    client = TestClient(satellite.app)
    body = {"sha": "abcdef1234567890", "changed_paths": []}
    assert client.post("/runtime/refresh", json=body).status_code == 401
    assert (
        client.post(
            "/runtime/refresh",
            json=body,
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )


def test_runtime_refresh_spawns_helper_with_sha_and_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_SATELLITE_REFRESH_SECRET", "refresh-secret")
    helper = tmp_path / "token-satellite-refresh"
    helper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setenv("TOKEN_SATELLITE_REFRESH_HELPER", str(helper))

    satellite = _load_satellite_module()
    spawned = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            spawned.append((args, kwargs))

    monkeypatch.setattr(satellite.subprocess, "Popen", FakePopen)

    from fastapi.testclient import TestClient

    client = TestClient(satellite.app)
    resp = client.post(
        "/runtime/refresh",
        json={
            "sha": "abcdef1234567890",
            "changed_paths": ["ahk/foo.ahk", "cli-tools/lib/nas-path.sh"],
        },
        headers={"Authorization": "Bearer refresh-secret"},
    )
    assert resp.status_code == 200, resp.text
    assert spawned
    args, kwargs = spawned[0]
    assert args[0] == str(helper)
    assert args[1] == "abcdef1234567890"
    manifest = Path(args[2])
    data = __import__("json").loads(manifest.read_text(encoding="utf-8"))
    assert data["changed_paths"] == ["ahk/foo.ahk", "cli-tools/lib/nas-path.sh"]
    assert kwargs["start_new_session"] is True


def test_tts_engine_synth_and_speak_plays_wav_artifact_not_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    satellite = _load_satellite_module()
    engine = satellite.TTSEngine()
    synth_result = {
        "success": True,
        "file_id": "fixed-id",
        "wav_path_win": r"C:\temp\tts\fixed-id.wav",
        "wav_path_wsl": str(tmp_path / "fixed-id.wav"),
        "message_chars": 12,
        "rendered_chars": 12,
        "rendered_hash": "a" * 64,
    }
    Path(synth_result["wav_path_wsl"]).write_bytes(b"RIFF")
    called = {}

    monkeypatch.setattr(engine, "synthesize", lambda message, voice, rate: synth_result)
    monkeypatch.setattr(engine, "speak", lambda *_args, **_kwargs: called.setdefault("speak", True))
    monkeypatch.setattr(
        engine,
        "_play_wav_file",
        lambda result: {**result, "success": True, "transport": "wsl_sapi_wav_file"},
    )

    result = engine.synth_and_speak("hello world!", "Microsoft David", 0)

    assert result["success"] is True
    assert result["transport"] == "wsl_sapi_wav_file"
    assert "speak" not in called


def test_tts_engine_wav_playback_returns_artifact_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    satellite = _load_satellite_module()
    engine = satellite.TTSEngine()
    wav_path = tmp_path / "fixed-id.wav"
    wav_path.write_bytes(b"RIFF")
    script_dir = tmp_path / "scripts"
    synth_result = {
        "file_id": "fixed-id",
        "wav_path_win": r"C:\temp\tts\fixed-id.wav",
        "wav_path_wsl": str(wav_path),
        "message_chars": 4,
        "rendered_chars": 4,
        "rendered_hash": "b" * 64,
    }
    popen_calls = []

    class FakePopen:
        pid = 4321
        returncode = 0

        def __init__(self, args, **kwargs):
            popen_calls.append((args, kwargs))

        def communicate(self, timeout=None):
            return ("", "")

        def poll(self):
            return self.returncode

    monkeypatch.setattr(engine, "PLAY_SCRIPT_DIR_WSL", str(script_dir))
    monkeypatch.setattr(engine, "PLAY_SCRIPT_DIR_WIN", r"C:\temp")
    monkeypatch.setattr(satellite.subprocess, "Popen", FakePopen)

    result = engine._play_wav_file(synth_result)

    assert result["success"] is True
    assert result["transport"] == "wsl_sapi_wav_file"
    assert result["file_id"] == "fixed-id"
    assert result["wav_path_win"] == r"C:\temp\tts\fixed-id.wav"
    assert result["playback_pid"] == 4321
    assert popen_calls[0][0][0] == satellite.POWERSHELL_EXE


def test_tts_engine_wav_stop_marks_playback_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    satellite = _load_satellite_module()
    engine = satellite.TTSEngine()
    terminated = {}

    class FakeProc:
        def poll(self):
            return None

        def terminate(self):
            terminated["terminate"] = True

        def wait(self, timeout=None):
            terminated["wait_timeout"] = timeout

    engine._playing = True
    engine._playback_process = FakeProc()

    assert engine.skip() is True
    assert engine._was_skipped is True
    assert terminated == {"terminate": True, "wait_timeout": 3}


def test_tts_engine_wav_pause_resume_returns_unsupported() -> None:
    satellite = _load_satellite_module()
    engine = satellite.TTSEngine()
    engine._playing = True
    engine._speaking = False

    result = engine.play_control("pause")

    assert result["success"] is False
    assert result["error"] == "unsupported_backend_control"
    assert result["transport"] == "wsl_sapi_wav_file"


def test_tts_engine_wav_playback_launch_error_returns_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    satellite = _load_satellite_module()
    engine = satellite.TTSEngine()
    wav_path = tmp_path / "fixed-id.wav"
    wav_path.write_bytes(b"RIFF")
    script_dir = tmp_path / "scripts"
    synth_result = {
        "file_id": "fixed-id",
        "wav_path_win": r"C:\temp\tts\fixed-id.wav",
        "wav_path_wsl": str(wav_path),
    }

    def fail_popen(*_args, **_kwargs):
        raise OSError("missing powershell")

    monkeypatch.setattr(engine, "PLAY_SCRIPT_DIR_WSL", str(script_dir))
    monkeypatch.setattr(engine, "PLAY_SCRIPT_DIR_WIN", r"C:\temp")
    monkeypatch.setattr(satellite.subprocess, "Popen", fail_popen)

    result = engine._play_wav_file(synth_result)

    assert result["success"] is False
    assert "Failed to start WAV playback" in result["error"]


def test_tts_engine_wav_playback_timeout_returns_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    satellite = _load_satellite_module()
    engine = satellite.TTSEngine()
    engine.MAX_PLAYBACK_SECONDS = 1
    wav_path = tmp_path / "fixed-id.wav"
    wav_path.write_bytes(b"RIFF")
    script_dir = tmp_path / "scripts"
    killed = {}
    synth_result = {
        "file_id": "fixed-id",
        "wav_path_win": r"C:\temp\tts\fixed-id.wav",
        "wav_path_wsl": str(wav_path),
    }

    class FakePopen:
        pid = 4321
        returncode = None

        def communicate(self, timeout=None):
            if timeout is not None:
                raise satellite.subprocess.TimeoutExpired(cmd="play", timeout=timeout)
            return ("", "")

        def kill(self):
            killed["kill"] = True
            self.returncode = -9

        def poll(self):
            return self.returncode

    monkeypatch.setattr(engine, "PLAY_SCRIPT_DIR_WSL", str(script_dir))
    monkeypatch.setattr(engine, "PLAY_SCRIPT_DIR_WIN", r"C:\temp")
    monkeypatch.setattr(satellite.subprocess, "Popen", lambda *_args, **_kwargs: FakePopen())

    result = engine._play_wav_file(synth_result)

    assert result == {"success": False, "error": "WAV playback timed out"}
    assert killed == {"kill": True}
