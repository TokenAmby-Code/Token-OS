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


def test_retired_overlay_buttons_are_disabled_stubs() -> None:
    expected = {
        "tts-overlay-pause.macro": "pause",
        "tts-overlay-resume.macro": "resume",
        "tts-overlay-skip.macro": "skip",
        "tts-overlay-faster.macro": "faster",
        "tts-overlay-stop.macro": "stop",
    }
    for filename, command in expected.items():
        macro = load(filename)
        assert macro["m_enabled"] is False
        triggers = macro["m_triggerList"]
        assert [t["m_classType"] for t in triggers] == ["FloatingButtonTrigger"]
        assert triggers[0]["identifier"] == f"tts-{command}"
        assert triggers[0]["m_isDisabled"] is True
        serialized = json.dumps(macro)
        assert "/api/tts/control" not in serialized
        assert "/tts-local-control" not in serialized


def test_notification_control_surface_replaces_floating_buttons() -> None:
    macro = load("tts-phone-control-notification.macro")
    trigger_types = [t["m_classType"] for t in macro["m_triggerList"]]
    assert trigger_types == ["HttpServerTrigger"] + ["NotificationButtonTrigger"] * 5
    assert macro["m_triggerList"][0]["identifier"] == "tts-control-surface"

    notification = actions(macro, "NotificationAction")[0]
    assert notification["notificationIdString"] == "token-os-tts-controls"
    assert notification["liveNotification"] is True
    assert notification["preventRemovalByBin"] is True
    assert [b["label"] for b in notification["notificationActionButtons"]] == [
        "Pause",
        "Resume",
        "Skip",
        "Faster",
        "Stop",
    ]

    urls = request_urls(macro)
    assert "http://127.0.0.1:7777/tts-control?command=pause&source=notification" in urls
    assert "http://127.0.0.1:7777/tts-control?command=resume&source=notification" in urls
    assert "http://127.0.0.1:7777/tts-control?command=skip&source=notification" in urls
    assert "http://127.0.0.1:7777/tts-control?command=faster&source=notification" in urls
    assert "http://127.0.0.1:7777/tts-control?command=stop&source=notification" in urls
    assert len(actions(macro, "FloatingButtonConfigureAction")) == 5


def test_control_ingress_forwards_to_token_os_first_and_does_not_mutate_locally() -> None:
    macro = load("tts-phone-control-ingress.macro")
    assert (
        token_os_global("tts-phone-control-ingress.macro")["m_stringValue"]
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


def test_local_control_echo_is_private_and_may_mutate_local_execution() -> None:
    macro = load("tts-phone-local-control.macro")
    assert macro["m_triggerList"][0]["identifier"] == "tts-local-control"
    assert request_urls(macro) == []
    serialized = json.dumps(macro)
    assert "local_control_consumed" in serialized
    assert "tts_control_state" in serialized
    assert "CancelActiveMacroAction" in serialized
    lowered = serialized.lower()
    assert "macos_say" not in lowered
    assert "mac say" not in lowered


def test_backfill_helper_calls_chunk_next_and_writes_global_handoff_state() -> None:
    macro = load("tts-phone-backfill-fetcher.macro")
    assert macro["m_triggerList"][0]["m_classType"] == "EmptyTrigger"
    assert request_urls(macro) == [f"{TOKEN_OS_BASE}/api/tts/chunk-next"]
    body = request_bodies(macro)[0]
    assert '"last_consumed_index":{v=tts_last_consumed_index}' in body
    assert '"session_id":"{v=tts_session_id}"' in body
    assert actions(macro, "JsonParseAction")[0]["dictionaryVarName"] == "tts_backfill_response"
    serialized = json.dumps(macro)
    assert "tts_backfill_status" in serialized
    assert "tts_backfill_next_chunk" in serialized
    assert "tts_backfill_next_index" in serialized


def test_chunk_player_streams_with_async_backfill_helper_and_boundary_promotion() -> None:
    macro = load("tts-phone-chunk-player.macro")
    assert (
        token_os_global("tts-phone-chunk-player.macro")["m_stringValue"]
        == "http://100.95.109.23:7777"
    )
    assert macro["m_triggerList"][0]["identifier"] == "tts-chunk"
    speak_actions = actions(macro, "SpeakTextAction")
    assert [a["m_textToSay"] for a in speak_actions] == ["{v=tts_current_chunk_text}"]
    assert speak_actions[0]["m_waitToFinish"] is True
    assert speak_actions[0]["m_queue"] is False

    force_runs = actions(macro, "ForceMacroRunAction")
    assert any(a["m_macroName"] == "TTS Phone Backfill Fetcher" and a["m_waitToComplete"] is False for a in force_runs)
    assert any(a["m_macroName"] == "TTS Phone Control Notification" for a in force_runs)

    serialized = json.dumps(macro)
    assert "{http_param=current_chunk}" in serialized
    assert "{http_param=next_chunk}" in serialized
    assert "m_textToSay\": \"{http_param" not in serialized
    assert "LoopAction" in serialized
    assert "tts_backfill_status" in serialized
    assert "current_complete_next_starting" in serialized
    assert "buffer_drained" in serialized
    assert len(actions(macro, "FloatingButtonConfigureAction")) == 5

    urls = request_urls(macro)
    assert f"{TOKEN_OS_BASE}/api/tts/chunk-event" in urls
    assert f"{TOKEN_OS_BASE}/api/tts/chunk-next" not in urls


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
