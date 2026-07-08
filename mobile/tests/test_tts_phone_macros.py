import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACROS = ROOT / "macros"
TOKEN_OS_BASE_VAR = "token_os_base_url"
TOKEN_OS_BASE = "{v=token_os_base_url}"
TOKEN_OS_CONTROL = f"{TOKEN_OS_BASE}/api/tts/control"


def load(name: str) -> dict:
    return json.loads((MACROS / name).read_text())["macro"]


def actions(macro: dict, class_type: str | None = None) -> list[dict]:
    items = macro["m_actionList"]
    if class_type:
        return [a for a in items if a.get("m_classType") == class_type]
    return items


def request_urls(macro: dict) -> list[str]:
    return [
        a["requestConfig"]["urlToOpen"] for a in actions(macro, "HttpRequestAction")
    ]


def request_bodies(macro: dict) -> list[str]:
    return [
        a["requestConfig"].get("contentBodyText", "")
        for a in actions(macro, "HttpRequestAction")
    ]


def load_wrapper(name: str) -> dict:
    return json.loads((MACROS / name).read_text())


def token_os_global(name: str) -> dict:
    wrapper = load_wrapper(name)
    matches = [
        v
        for v in wrapper.get("globalVariables", [])
        if v.get("m_name") == TOKEN_OS_BASE_VAR
    ]
    assert len(matches) == 1
    return matches[0]


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
        assert (
            actions(macro, "HttpRequestAction")[0]["requestConfig"][
                "requestTimeOutSeconds"
            ]
            == 12
        )
        serialized = json.dumps(macro)
        assert "/tts-local-control" not in serialized
        assert "/api/tts/control" not in serialized
        assert "SetVariableAction" not in serialized
        assert "CancelActiveMacroAction" not in serialized


def test_control_ingress_forwards_to_token_os_first_and_does_not_mutate_locally() -> (
    None
):
    macro = load("tts-phone-control-ingress.macro")
    assert (
        token_os_global("tts-phone-control-ingress.macro")["m_stringValue"]
        == "http://100.95.109.23:7777"
    )
    assert macro["m_triggerList"][0]["identifier"] == "tts-control"
    assert request_urls(macro) == [TOKEN_OS_CONTROL]
    body = request_bodies(macro)[0]
    assert '"command":"{http_param=command}"' in body
    assert '"source":"phone_overlay"' in body
    assert '"backend":"phone"' in body
    assert '"speed":"{http_param=speed}"' in body
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


def test_chunk_player_streams_with_scalar_speak_text_and_backfill() -> None:
    macro = load("tts-phone-chunk-player.macro")
    assert macro["m_name"] == "04 TTS Chunk Player"
    assert (
        token_os_global("tts-phone-chunk-player.macro")["m_stringValue"]
        == "http://100.95.109.23:7777"
    )
    assert macro["m_triggerList"][0]["identifier"] == "tts-chunk"
    speak_actions = actions(macro, "SpeakTextAction")
    assert [a["m_textToSay"] for a in speak_actions] == [
        "{lv=current_chunk_text}",
        "{lv=next_chunk_text}",
        "{lv=backfill_next_chunk}",
    ]
    assert [a["m_queue"] for a in speak_actions] == [False, True, True]
    assert speak_actions[0]["m_waitToFinish"] is True
    assert speak_actions[1]["m_waitToFinish"] is False

    serialized = json.dumps(macro)
    # Only scalar locals are fed to SpeakTextAction; request/backfill dictionaries
    # are dereferenced outside TTS so MacroDroid does not speak literal variables.
    assert all("http_param=" not in a["m_textToSay"] for a in speak_actions)
    assert all("request[" not in a["m_textToSay"] for a in speak_actions)
    assert all("backfill[" not in a["m_textToSay"] for a in speak_actions)
    assert ("current_chunk_text", "{lv=request[current_chunk]}") in [
        (a["m_variable"]["m_name"], a.get("m_newStringValue", ""))
        for a in actions(macro, "SetVariableAction")
    ]
    assert "SetVariableAction" in serialized
    assert "LoopAction" in serialized
    assert "PauseAction" not in serialized
    assert "ForceMacroRunAction" not in serialized
    assert "m_textToSay\": \"{lv=request" not in serialized
    assert "streaming_current_plus_next" in serialized
    assert "done={lv=backfill_done}" in serialized

    urls = request_urls(macro)
    assert urls.count(f"{TOKEN_OS_BASE}/api/tts/chunk-event") >= 3
    assert f"{TOKEN_OS_BASE}/api/tts/chunk-next" in urls
    bodies = request_bodies(macro)
    assert "current_complete_next_starting" in bodies[0]
    assert '"last_consumed_index":"{lv=current_index}"' in bodies[1]
    assert sum("buffer_drained" in body for body in bodies) >= 2
    assert (
        actions(macro, "HttpRequestAction")[0]["requestConfig"]["blockNextAction"]
        is False
    )
    assert (
        actions(macro, "HttpRequestAction")[1]["requestConfig"]["responseVariableName"]
        == "backfill_raw"
    )

    set_actions = actions(macro, "SetVariableAction")
    assignments = [
        (a["m_variable"]["m_name"], a.get("m_newStringValue", ""))
        for a in set_actions
    ]
    assert ("current_index", "{lv=next_index}") in assignments
    assert ("next_index", "{lv=backfill_next_index}") in assignments
    assert ("backfill_done", "{lv=backfill[done]}") in assignments


def test_error_report_goes_up_to_token_os_and_has_no_mac_fallback() -> None:
    macro = load("tts-phone-error-report.macro")
    assert (
        token_os_global("tts-phone-error-report.macro")["m_stringValue"]
        == "http://100.95.109.23:7777"
    )
    assert macro["m_triggerList"][0]["identifier"] == "tts-error"
    assert request_urls(macro) == [f"{TOKEN_OS_BASE}/api/tts/backend-error"]
    body = request_bodies(macro)[0]
    assert body == '{"backend":"phone","request":{lv=tts_error_request_json}}'
    assert actions(macro, "JsonOutputAction")[0]["dictionaryVarName"] == "request"
    assert (
        actions(macro, "JsonOutputAction")[0]["stringVarName"]
        == "tts_error_request_json"
    )
    assert "{lv=request[error_code]}" not in body
    serialized = json.dumps(macro).lower()
    assert "macos" not in serialized
    assert "mac say" not in serialized
    assert "fallback" not in serialized.replace("no mac or local fallback", "")
