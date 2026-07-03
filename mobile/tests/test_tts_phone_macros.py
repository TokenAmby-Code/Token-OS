import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACROS = ROOT / "macros"
TOKEN_OS_CONTROL = "http://100.95.109.23:7777/api/tts/control"


def load(name: str) -> dict:
    return json.loads((MACROS / name).read_text())["macro"]


def actions(macro: dict, class_type: str | None = None) -> list[dict]:
    items = macro["m_actionList"]
    if class_type:
        return [a for a in items if a.get("m_classType") == class_type]
    return items


def request_urls(macro: dict) -> list[str]:
    return [
        a["requestConfig"]["urlToOpen"]
        for a in actions(macro, "HttpRequestAction")
    ]


def request_bodies(macro: dict) -> list[str]:
    return [
        a["requestConfig"].get("contentBodyText", "")
        for a in actions(macro, "HttpRequestAction")
    ]


def test_overlay_buttons_call_local_ingress_not_token_os_or_local_control() -> None:
    expected = {
        "tts-overlay-pause.macro": "pause",
        "tts-overlay-resume.macro": "resume",
        "tts-overlay-skip.macro": "skip",
        "tts-overlay-faster.macro": "faster",
        "tts-overlay-stop.macro": "stop",
    }
    for filename, command in expected.items():
        macro = load(filename)
        triggers = macro["m_triggerList"]
        assert [t["m_classType"] for t in triggers] == ["FloatingButtonTrigger"]
        assert triggers[0]["identifier"] == f"tts-{command}"
        assert request_urls(macro) == [
            f"http://127.0.0.1:7777/tts-control?command={command}&source=overlay"
        ]
        serialized = json.dumps(macro)
        assert "/tts-local-control" not in serialized
        assert "/api/tts/control" not in serialized
        assert "SetVariableAction" not in serialized
        assert "CancelActiveMacroAction" not in serialized


def test_control_ingress_forwards_to_token_os_first_and_does_not_mutate_locally() -> None:
    macro = load("tts-phone-control-ingress.macro")
    assert macro["m_triggerList"][0]["identifier"] == "tts-control"
    assert request_urls(macro) == [TOKEN_OS_CONTROL]
    body = request_bodies(macro)[0]
    assert '"command":"{lv=request[command]}"' in body
    assert '"source":"phone_overlay"' in body
    assert '"backend":"phone"' in body
    serialized = json.dumps(macro)
    forbidden_local_mutations = [
        "SpeakTextAction",
        "SetVariableAction",
        "CancelActiveMacroAction",
        "ForceMacroRunAction",
        "ControlMediaAction",
    ]
    for class_type in forbidden_local_mutations:
        assert class_type not in serialized


def test_local_control_echo_is_private_consumed_endpoint_not_authority() -> None:
    macro = load("tts-phone-local-control.macro")
    assert macro["m_triggerList"][0]["identifier"] == "tts-local-control"
    assert request_urls(macro) == []
    serialized = json.dumps(macro)
    assert "local_control_consumed" in serialized
    lowered = serialized.lower()
    assert "macos_say" not in lowered
    assert "mac say" not in lowered


def test_chunk_player_is_exactly_one_chunk_write_ahead_no_local_queue() -> None:
    macro = load("tts-phone-chunk-player.macro")
    assert macro["m_triggerList"][0]["identifier"] == "tts-chunk"
    speak_actions = actions(macro, "SpeakTextAction")
    assert [a["m_textToSay"] for a in speak_actions] == [
        "{lv=request[current_chunk]}",
        "{lv=request[next_chunk]}",
    ]
    assert all(a["m_waitToFinish"] is True for a in speak_actions)
    assert all(a["m_queue"] is False for a in speak_actions)

    serialized = json.dumps(macro)
    # The phone is an executor, not a queue owner.
    assert "LoopAction" not in serialized
    assert "IterateDictionaryAction" not in serialized
    assert "ForceMacroRunAction" not in serialized
    assert "SetVariableAction" not in serialized
    assert "queue" not in macro["m_description"].lower().replace("no local queue", "")
    assert "current_plus_next" in serialized

    urls = request_urls(macro)
    assert urls == [
        "http://100.95.109.23:7777/api/tts/chunk-event",
        "http://100.95.109.23:7777/api/tts/chunk-event",
    ]
    assert "current_complete_next_starting" in request_bodies(macro)[0]
    assert "buffer_drained" in request_bodies(macro)[1]


def test_error_report_goes_up_to_token_os_and_has_no_mac_fallback() -> None:
    macro = load("tts-phone-error-report.macro")
    assert macro["m_triggerList"][0]["identifier"] == "tts-error"
    assert request_urls(macro) == ["http://100.95.109.23:7777/api/tts/backend-error"]
    body = request_bodies(macro)[0]
    assert '"backend":"phone"' in body
    assert '"error_code":"{lv=request[error_code]}"' in body
    serialized = json.dumps(macro).lower()
    assert "macos" not in serialized
    assert "mac say" not in serialized
    assert "fallback" not in serialized.replace("no mac or local fallback", "")
