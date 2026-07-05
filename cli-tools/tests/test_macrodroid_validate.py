from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path


def load_macrodroid_validate():
    script = Path(__file__).resolve().parents[1] / "bin" / "macrodroid-validate"
    loader = importlib.machinery.SourceFileLoader("macrodroid_validate_bin", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def wrapper(text: str) -> dict:
    return {
        "macroExportVersion": 1,
        "globalVariables": [],
        "userIcons": None,
        "aiFeedback": "test",
        "macro": {
            "m_name": "TTS Probe",
            "m_enabled": False,
            "m_completed": True,
            "m_GUID": 0,
            "aiGenerated": 1,
            "m_description": "probe",
            "m_triggerList": [{"m_classType": "EmptyTrigger", "m_SIGUID": 0}],
            "m_actionList": [
                {"m_classType": "SpeakTextAction", "m_SIGUID": 0, "m_textToSay": text}
            ],
            "m_constraintList": [],
        },
    }


def errors_for(text: str) -> list[str]:
    module = load_macrodroid_validate()
    errors, _warnings = module.validate(
        wrapper(text), {"Trigger": set(), "Action": set(), "Constraint": set()}
    )
    return errors


def test_speak_text_allows_literal_string() -> None:
    assert errors_for("literal chunk text") == []


def test_speak_text_allows_scalar_local_variable() -> None:
    assert errors_for("{lv=current_chunk_text}") == []


def test_speak_text_rejects_direct_http_param_magic() -> None:
    errors = errors_for("{http_param=current_chunk}")
    assert any("direct HTTP magic text" in e for e in errors)
    assert any("unsupported magic text" in e for e in errors)


def test_speak_text_rejects_local_dictionary_lookup() -> None:
    errors = errors_for("{lv=request[current_chunk]}")
    assert any("must not dereference dictionaries directly" in e for e in errors)


def test_speak_text_rejects_global_magic_text() -> None:
    errors = errors_for("{v=tts_text}")
    assert any("must not use global-variable magic text" in e for e in errors)
