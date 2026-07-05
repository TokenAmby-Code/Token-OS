from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path


def load_macrodroid_import():
    script = Path(__file__).resolve().parents[1] / "bin" / "macrodroid-import"
    loader = importlib.machinery.SourceFileLoader("macrodroid_import_bin", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def macro(name: str = "Probe", *, guid: int = 0, text: str = "hello") -> dict:
    return {
        "m_name": name,
        "m_enabled": False,
        "m_completed": True,
        "m_GUID": guid,
        "aiGenerated": 1,
        "m_description": "probe",
        "m_triggerList": [{"m_classType": "EmptyTrigger", "m_SIGUID": 100}],
        "m_actionList": [
            {
                "m_classType": "LogAction",
                "m_SIGUID": 101,
                "m_logText": text,
                "m_constraintList": [],
            }
        ],
        "m_constraintList": [],
    }


def state(*macros: dict) -> dict:
    return {"macroList": list(macros)}


def evaluate(module, *, replace=False, allow_existing=False, before=(), after=(), candidate=None):
    candidate = candidate or macro()
    return module.evaluate_import_result(
        replace=replace,
        allow_existing=allow_existing,
        candidate_macro=candidate,
        before_state=state(*before),
        after_state=state(*after),
        before_count=len(before),
        after_count=len(after),
    )


def test_semantic_fingerprint_ignores_guid_and_candidate_absent_live_defaults() -> None:
    module = load_macrodroid_import()
    candidate = macro(guid=0)
    live = macro(guid=12345)
    live["lastEditedTimestamp"] = 999
    live["m_actionList"][0]["extraLiveDefault"] = "ignored because candidate did not provide it"
    live["m_actionList"][0]["m_SIGUID"] = 99999

    assert module.semantic_matches(candidate, live) is True


def test_semantic_fingerprint_ignores_variable_current_values() -> None:
    module = load_macrodroid_import()
    candidate = macro()
    candidate["m_actionList"].append(
        {
            "m_classType": "SetVariableAction",
            "m_SIGUID": 102,
            "m_newBooleanValue": True,
            "m_variable": {
                "m_name": "gate",
                "m_type": 0,
                "m_booleanValue": True,
                "isLocalVar": False,
                "supportsInput": True,
                "supportsOutput": True,
            },
        }
    )
    live = macro(guid=12345)
    live["m_actionList"].append(
        {
            "m_classType": "SetVariableAction",
            "m_SIGUID": 999,
            "m_newBooleanValue": True,
            "m_variable": {
                "m_name": "gate",
                "m_type": 0,
                "m_booleanValue": False,
                "isLocalVar": False,
                "supportsInput": True,
                "supportsOutput": True,
            },
        }
    )

    assert module.semantic_matches(candidate, live) is True


def test_existing_macro_unchanged_but_export_count_or_hash_moves_fails() -> None:
    module = load_macrodroid_import()
    candidate = macro()
    existing = macro(guid=42)
    unrelated = macro("Unrelated", guid=99)

    success, wiped, diagnostics = evaluate(
        module,
        candidate=candidate,
        before=[existing],
        after=[existing, unrelated],
    )

    assert success is False
    assert wiped is False
    assert diagnostics["new_semantic"] == 0


def test_default_import_requires_one_new_semantic_match() -> None:
    module = load_macrodroid_import()
    candidate = macro()
    live = macro(guid=42)

    success, wiped, diagnostics = evaluate(module, candidate=candidate, before=[], after=[live])

    assert success is True
    assert wiped is False
    assert diagnostics["after_semantic"] == 1
    assert diagnostics["new_semantic"] == 1


def test_candidate_live_content_mismatch_fails_even_with_name_and_count_match() -> None:
    module = load_macrodroid_import()
    candidate = macro(text="intended")
    live = macro(guid=42, text="wrong")

    success, wiped, diagnostics = evaluate(module, candidate=candidate, before=[], after=[live])

    assert success is False
    assert wiped is False
    assert diagnostics["after_same_name"] == 1
    assert diagnostics["after_semantic"] == 0


def test_allow_existing_accepts_one_added_semantic_duplicate() -> None:
    module = load_macrodroid_import()
    candidate = macro()
    existing = macro(guid=41)
    added = macro(guid=42)

    success, wiped, diagnostics = evaluate(
        module,
        allow_existing=True,
        candidate=candidate,
        before=[existing],
        after=[existing, added],
    )

    assert success is True
    assert wiped is False
    assert diagnostics["new_semantic"] == 1


def test_replace_can_reduce_total_when_duplicates_are_cleaned() -> None:
    module = load_macrodroid_import()
    candidate = macro()
    old_one = macro(guid=41, text="old")
    old_two = macro(guid=42, text="old")
    replacement = macro(guid=43)

    success, wiped, diagnostics = evaluate(
        module,
        replace=True,
        candidate=candidate,
        before=[old_one, old_two, macro("Other", guid=50)],
        after=[replacement, macro("Other", guid=50)],
    )

    assert success is True
    assert wiped is False
    assert diagnostics["after_same_name"] == 1
    assert diagnostics["after_semantic"] == 1


def test_replace_duplicate_remains_fails_with_duplicate_details() -> None:
    module = load_macrodroid_import()
    candidate = macro()
    old_one = macro(guid=41, text="old")
    old_two = macro(guid=42, text="old")
    replacement = macro(guid=43)

    success, wiped, diagnostics = evaluate(
        module,
        replace=True,
        candidate=candidate,
        before=[old_one, old_two],
        after=[old_one, replacement],
    )

    assert success is False
    assert wiped is False
    assert diagnostics["after_same_name"] == 2
    assert diagnostics["after_semantic"] == 1
