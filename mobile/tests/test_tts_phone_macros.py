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


def set_variable_assignments(macro: dict) -> list[tuple[str, str]]:
    return [
        (a["m_variable"]["m_name"], a.get("m_newStringValue", ""))
        for a in actions(macro, "SetVariableAction")
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


def test_artifact_player_shell_fetches_wav_plays_and_reports_buffer_drained() -> None:
    macro = load("tts-phone-chunk-player.macro")
    assert macro["m_name"] == "04 TTS Artifact Player"
    assert (
        token_os_global("tts-phone-chunk-player.macro")["m_stringValue"]
        == "http://100.95.109.23:7777"
    )
    assert macro["m_triggerList"][0]["identifier"] == "tts-artifact"
    assert actions(macro, "SpeakTextAction") == []

    tts_dir = "/storage/emulated/0/MacroDroid/tts"
    wav = f"{tts_dir}/token-tts-current.wav"
    scripts = [a["m_script"] for a in actions(macro, "ShellScriptAction")]
    assert len(scripts) == 3

    entry, fetch, finished = scripts
    assert f"mkdir -p {tts_dir}" in entry
    assert f"rm -f {wav}" in entry
    assert "{lv=request[session_id]}" in entry
    assert "{lv=request[playback_id]}" in entry
    # {http_param=...} does not expand inside shell scripts (verified on-device)
    assert all("{http_param=" not in s for s in scripts)

    # MacroDroid's native save-response path writes nothing on this device;
    # the shell fetch is the artifact transport. Keep the dead path excised.
    assert '"{lv=request[artifact_url]}"' in fetch
    assert "curl -s -o" in fetch
    assert "wget -q -O" in fetch
    assert wav in fetch
    for action in actions(macro, "HttpRequestAction"):
        assert action["requestConfig"]["saveResponseType"] == 0

    assert "playback finished" in finished

    play_actions = actions(macro, "PlaySoundAction")
    assert len(play_actions) == 1
    assert play_actions[0]["waitToFinish"] is True
    assert play_actions[0]["useAllFilesAccess"] is True
    assert play_actions[0]["allFilesPath"] == tts_dir
    assert play_actions[0]["allFilesFilename"] == "token-tts-current.wav"

    assignments = dict(set_variable_assignments(macro))
    assert assignments["session_id"] == "{lv=request[session_id]}"
    assert assignments["playback_id"] == "{lv=request[playback_id]}"

    serialized = json.dumps(macro)
    assert "LoopAction" not in serialized
    assert "/api/tts/chunk-next" not in serialized
    assert "SpeakTextAction" not in serialized

    urls = request_urls(macro)
    assert urls == [f"{TOKEN_OS_BASE}/api/tts/chunk-event"]
    bodies = [body for body in request_bodies(macro) if body]
    assert len(bodies) == 1
    assert "buffer_drained" in bodies[0]


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
