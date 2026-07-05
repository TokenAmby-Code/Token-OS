import json
from pathlib import Path

MACRO = (
    Path(__file__).resolve().parents[1] / "macros" / "macrodroid-import-harness.macro"
)


def load_macro() -> dict:
    return json.loads(MACRO.read_text())["macro"]


def actions(macro: dict, class_type: str | None = None) -> list[dict]:
    items = macro["m_actionList"]
    if class_type:
        return [a for a in items if a.get("m_classType") == class_type]
    return items


def test_import_harness_exposes_only_gated_import_endpoints() -> None:
    macro = load_macro()
    assert macro["m_name"] == "MacroDroid Import Harness"
    assert [t["identifier"] for t in macro["m_triggerList"]] == [
        "macrodroid-import-arm",
        "macrodroid-import-disarm",
        "macrodroid-import-accept",
    ]


def test_import_harness_click_is_bottom_right_percentage_and_gated() -> None:
    macro = load_macro()
    clicks = actions(macro, "UIInteractionAction")
    assert len(clicks) == 1
    cfg = clicks[0]["uiInteractionConfiguration"]
    assert cfg["clickOption"] == 2
    assert cfg["xyPercentages"] is True
    assert cfg["xyPoint"] == {"x": 88, "y": 93}

    serialized = json.dumps(macro)
    assert "md_import_auto_accept_enabled" in serialized
    assert "StopWatchConstraint" in serialized
    assert "macrodroid_import_auto_accept_ttl" in serialized
    assert "m_timePeriodSeconds" in serialized
    assert "1800" in serialized


def test_import_harness_logs_arm_click_and_refusal() -> None:
    macro = load_macro()
    scripts = "\n".join(
        a.get("m_script", "") for a in actions(macro, "ShellScriptAction")
    )
    assert "/storage/emulated/0/MacroDroid/logs/debug.log" in scripts
    assert "armed ttl_seconds=1800" in scripts
    assert "clicked import prompt" in scripts
    assert "refused click gate_disabled_or_expired" in scripts
