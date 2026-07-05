import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACROS = ROOT / "macros"
TOKEN_OS_BASE_VAR = "token_os_base_url"
TOKEN_OS_BASE = "{v=token_os_base_url}"
TOKEN_OS_CONTROL = f"{TOKEN_OS_BASE}/api/tts/control"


def load(name: str) -> dict:
    return json.loads((MACROS / name).read_text())["macro"]


def load_wrapper(name: str) -> dict:
    return json.loads((MACROS / name).read_text())


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


def token_os_global(name: str) -> dict:
    wrapper = load_wrapper(name)
    matches = [
        v
        for v in wrapper.get("globalVariables", [])
        if v.get("m_name") == TOKEN_OS_BASE_VAR
    ]
    assert len(matches) == 1
    return matches[0]


def test_controls_notification_uses_direct_button_actions_not_button_triggers() -> None:
    macro = load("01-controls-notification.macro")
    assert macro["m_name"] == "01 TTS Controls Notification"
    assert [t["m_classType"] for t in macro["m_triggerList"]] == [
        "HttpServerTrigger"
    ]
    assert macro["m_triggerList"][0]["identifier"] == "tts-control-surface"

    notification = actions(macro, "NotificationAction")[0]
    buttons = notification["notificationActionButtons"]
    assert [b["label"] for b in buttons] == [
        "Pause",
        "Resume",
        "Skip",
        "Faster",
        "Stop",
    ]
    for button in buttons:
        assert button["macroGuid"] == 0
        assert button["macroName"] == ""
        assert button["actionBlockData"] is None
        assert button["clearOnPress"] is False
        assert button["actionClassType"] == "HttpRequestAction"
        action = json.loads(button["actionJson"])
        assert action["m_classType"] == "HttpRequestAction"
        url = action["requestConfig"]["urlToOpen"]
        command = button["label"].lower()
        assert url == (
            f"http://127.0.0.1:7777/tts-control?command={command}"
            "&source=notification"
        )
        assert action["requestConfig"]["requestType"] == 0

    serialized = json.dumps(macro)
    assert "NotificationButtonTrigger" not in serialized
    assert "TriggerThatInvokedConstraint" not in serialized
    assert "/api/tts/control" not in serialized
    assert "/tts-local-control" not in serialized


def test_control_ingress_forwards_to_token_os_first_and_does_not_mutate_locally() -> (
    None
):
    macro = load("02-control-ingress.macro")
    assert (
        token_os_global("02-control-ingress.macro")["m_stringValue"]
        == "http://100.95.109.23:7777"
    )
    assert macro["m_triggerList"][0]["identifier"] == "tts-control"
    assert request_urls(macro) == [TOKEN_OS_CONTROL]
    body = request_bodies(macro)[0]
    assert '"command":"{http_param=command}"' in body
    assert '"source":"phone_notification"' in body
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


def test_local_control_echo_is_private_execution_authority() -> None:
    macro = load("03-local-echo-control.macro")
    assert macro["m_triggerList"][0]["identifier"] == "tts-local-control"
    assert request_urls(macro) == []
    serialized = json.dumps(macro)
    assert "local_control_consumed" in serialized
    assert "tts_control_state" in serialized
    assert "CancelActiveMacroAction" in serialized
    lowered = serialized.lower()
    assert "macos_say" not in lowered
    assert "mac say" not in lowered


def test_numbered_tts_set_is_the_only_active_source() -> None:
    active = sorted(p.name for p in MACROS.glob("*.macro"))
    assert active == [
        "01-controls-notification.macro",
        "02-control-ingress.macro",
        "03-local-echo-control.macro",
        "04-chunk-player.macro",
        "05-backfill-fetcher.macro",
        "06-error-report.macro",
        "pause.macro",
        "zappa-single-lane.macro",
    ]
    retired_prefixes = ("tts-phone-", "tts-overlay-", "90-", "91-", "92-", "93-", "94-")
    assert not any(name.startswith(retired_prefixes) for name in active)


def test_chunk_player_uses_request_dictionary_deref_scalar_speak_and_inloop_backfill() -> None:
    macro = load("04-chunk-player.macro")
    assert (
        token_os_global("04-chunk-player.macro")["m_stringValue"]
        == "http://100.95.109.23:7777"
    )
    trigger = macro["m_triggerList"][0]
    assert trigger["identifier"] == "tts-chunk"
    assert trigger["queryParamsDictionaryName"] == "request"

    serialized = json.dumps(macro)
    assert "{lv=request[current_chunk]}" in serialized
    assert "{lv=request[next_chunk]}" in serialized
    assert "{lv=request[current_index]}" in serialized
    assert "{lv=backfill[next_chunk]}" in serialized
    assert "{lv=backfill[next_index]}" in serialized
    assert "{lv=backfill[control_state]}" in serialized

    speak_actions = actions(macro, "SpeakTextAction")
    assert [a["m_textToSay"] for a in speak_actions] == [
        "{lv=current_chunk_text}",
        "{lv=next_chunk_text}",
        "{lv=backfill_next_chunk}",
    ]
    assert [a["m_queue"] for a in speak_actions] == [False, True, True]
    assert speak_actions[0]["m_waitToFinish"] is True
    assert speak_actions[1]["m_waitToFinish"] is False
    assert speak_actions[2]["m_waitToFinish"] is True
    for speak in speak_actions:
        assert "{http_param=" not in speak["m_textToSay"]
        assert "{v=tts_" not in speak["m_textToSay"]
        assert speak["m_textToSay"].startswith("{lv=")

    assert "{http_param=current_chunk}" not in serialized
    assert "{http_param=next_chunk}" not in serialized
    assert "IterateDictionaryAction" not in serialized
    assert "JsonParseAction" in serialized
    assert "LoopAction" in serialized
    assert "05 TTS Backfill Fetcher" not in json.dumps(actions(macro, "ForceMacroRunAction"))

    urls = request_urls(macro)
    assert urls.count(f"{TOKEN_OS_BASE}/api/tts/chunk-next") == 1
    assert urls.count(f"{TOKEN_OS_BASE}/api/tts/chunk-event") == 4
    bodies = request_bodies(macro)
    assert any("current_complete_next_starting" in body for body in bodies)
    assert any("buffer_drained" in body and "control_stop" in body for body in bodies)
    assert any("buffer_drained" in body for body in bodies)
    chunk_next = actions(macro, "HttpRequestAction")[1]
    assert chunk_next["requestConfig"]["responseVariableName"] == "backfill_raw"
    assert "last_consumed_index" in chunk_next["requestConfig"]["contentBodyText"]


def test_backfill_fetcher_uses_direct_json_parse_not_dictionary_iteration() -> None:
    macro = load("05-backfill-fetcher.macro")
    assert request_urls(macro) == [f"{TOKEN_OS_BASE}/api/tts/chunk-next"]
    body = request_bodies(macro)[0]
    assert "last_consumed_index" in body
    serialized = json.dumps(macro)
    assert "JsonParseAction" in serialized
    assert "IterateDictionaryAction" not in [a.get("m_classType") for a in actions(macro)]
    assert "tts_backfill_response[next_chunk]" in serialized
    assert "tts_backfill_response[next_index]" in serialized


def test_error_report_goes_up_to_token_os_and_has_no_mac_fallback() -> None:
    macro = load("06-error-report.macro")
    assert (
        token_os_global("06-error-report.macro")["m_stringValue"]
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
    serialized = json.dumps(macro).lower()
    assert "macos" not in serialized
    assert "mac say" not in serialized
    assert "fallback" not in serialized.replace("no mac or local fallback", "")
