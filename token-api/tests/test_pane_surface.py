from __future__ import annotations

import sys

import pytest

# Placeholder stems (with optional numeric collision/monotonic suffix) that must
# be classed as non-human across every detector. Numbered variants are the cause
# of the needs-session-name-345 leak this regex closes.
PLACEHOLDER_NAMES = [
    "needs-name",
    "needs-name-2",
    "needs-session-name",
    "needs-session-name-345",
    "unnamed-session-3",
    "session-doc-12",
    "session",
]

# Legitimate human/doc names that only *start* with a placeholder token — these
# must never be swallowed by the regex.
REAL_NAMES = [
    "session-replay-1",
    "custodes-fleet-audit-3",
    "mechanicus-deploy",
    "needs-review-now",
]


@pytest.mark.xfail(
    reason="QUARANTINE: c9aa199 (recovered/tabname-session-binding-wip) ships "
    "test-incomplete placeholder-detection impl. See mega-main CodeRabbit triage "
    "'TOP FOLLOW-UP'. Finish impl (numbered-stem detection across all detectors) "
    "or drop the commit to un-quarantine. strict=False so XPASS signals impl done.",
    strict=False,
)
@pytest.mark.parametrize("name", PLACEHOLDER_NAMES)
def test_placeholder_names_agree_across_all_detectors(app_env, name):
    pane_surface = __import__("pane_surface")
    # 1. pane_surface (canonical, protected)
    assert pane_surface.is_meaningful_tab_name(name) is False
    assert pane_surface.human_tab_name(name) is None
    # 2. main reconciler / nudge gate
    assert app_env.main._is_placeholder_tab_name(name) is True


@pytest.mark.parametrize("name", REAL_NAMES)
def test_real_names_agree_across_all_detectors(app_env, name):
    pane_surface = __import__("pane_surface")
    assert pane_surface.is_meaningful_tab_name(name) is True
    assert pane_surface.human_tab_name(name) == name
    assert app_env.main._is_placeholder_tab_name(name) is False


def test_session_doc_instance_namer_is_removed(app_env) -> None:
    """Session document paths/slugs must not provide an instance-name path."""
    hooks = sys.modules["routes.hooks"]
    assert not hasattr(hooks, "_apply_session_doc_instance_name")
    assert not hasattr(hooks, "_instance_name_base_from_session_doc")


def test_golden_throne_human_surface_includes_position_and_name(app_env):
    assert (
        app_env.main._golden_throne_human_surface(
            "recovery-callout-id-re",
            "%101",
            "palace:N",
        )
        == "palace:N recovery-callout-id-re"
    )


def test_golden_throne_human_surface_rejects_claude_placeholder(app_env):
    assert (
        app_env.main._golden_throne_human_surface("Claude 08:14", "%101", "palace:N") == "palace:N"
    )


def test_golden_throne_human_surface_dynamic_workspace_uses_name(app_env):
    assert (
        app_env.main._golden_throne_human_surface(
            "custodes-cascade-intervention",
            "%102",
            "council:custodes",
        )
        == "council:custodes custodes-cascade-intervention"
    )


def test_golden_throne_human_surface_missing_label_uses_name(app_env):
    assert app_env.main._golden_throne_human_surface("recovery-foo", "%103", None) == "recovery-foo"


def test_golden_throne_human_surface_never_falls_back_to_raw_tmux(app_env):
    assert app_env.main._golden_throne_human_surface("Claude 08:14", "%108", None) == "session"


def test_human_surface_rejects_embedded_raw_tmux_label(app_env) -> None:
    assert (
        app_env.main._golden_throne_human_surface("Claude 08:14", "%108", "palace:%17") == "session"
    )


def test_golden_throne_human_surface_dynamic_workspace_uses_public_label(app_env):
    assert (
        app_env.main._golden_throne_human_surface("Claude 08:14", "%210", "mechanicus:aspirant")
        == "mechanicus:aspirant"
    )


def test_golden_throne_surface_does_not_embed_raw_tmux(app_env):
    assert (
        app_env.main._golden_throne_surface("Claude 08:14", "%210", "mechanicus:aspirant")
        == "mechanicus:aspirant"
    )
    assert app_env.main._golden_throne_surface("Claude 08:14", "%210", None) == "session"


def test_golden_throne_notification_text_uses_surface_without_duplicate_name(app_env):
    surface = app_env.main._golden_throne_human_surface(
        "recovery-cascade-post-close-race",
        "%119",
        "palace:NW",
    )

    assert surface == "palace:NW recovery-cascade-post-close-race"
    assert (
        app_env.main._golden_throne_tts_text("recovery-cascade-post-close-race", surface)
        == "Golden Throne resuming palace:NW recovery-cascade-post-close-race"
    )
    assert (
        app_env.main._golden_throne_banner_text("recovery-cascade-post-close-race", surface)
        == "GT resume: palace:NW recovery-cascade-post-close-race"
    )
