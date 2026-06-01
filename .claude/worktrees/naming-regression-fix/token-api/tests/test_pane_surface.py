from __future__ import annotations


def test_golden_throne_human_surface_includes_position_and_name(app_env):
    assert (
        app_env.main._golden_throne_human_surface(
            "recovery-callout-id-re",
            "%101",
            "palace:N",
        )
        == "1:N recovery-callout-id-re"
    )


def test_golden_throne_human_surface_rejects_claude_placeholder(app_env):
    assert app_env.main._golden_throne_human_surface("Claude 08:14", "%101", "palace:N") == "1:N"


def test_golden_throne_human_surface_dynamic_workspace_uses_name(app_env):
    assert (
        app_env.main._golden_throne_human_surface(
            "custodes-cascade-intervention",
            "%102",
            "legion:custodes",
        )
        == "custodes-cascade-intervention"
    )


def test_golden_throne_human_surface_missing_label_uses_name(app_env):
    assert app_env.main._golden_throne_human_surface("recovery-foo", "%103", None) == "recovery-foo"


def test_golden_throne_human_surface_never_falls_back_to_raw_tmux(app_env):
    assert app_env.main._golden_throne_human_surface("Claude 08:14", "%108", None) == "session"


def test_golden_throne_human_surface_dynamic_workspace_uses_public_label(app_env):
    assert (
        app_env.main._golden_throne_human_surface("Claude 08:14", "%210", "legion:aspirant")
        == "legion:aspirant"
    )


def test_golden_throne_surface_does_not_embed_raw_tmux(app_env):
    assert (
        app_env.main._golden_throne_surface("Claude 08:14", "%210", "legion:aspirant")
        == "legion:aspirant"
    )
    assert app_env.main._golden_throne_surface("Claude 08:14", "%210", None) == "Claude 08:14"


def test_golden_throne_notification_text_uses_surface_without_duplicate_name(app_env):
    surface = app_env.main._golden_throne_human_surface(
        "recovery-cascade-post-close-race",
        "%119",
        "palace:NW",
    )

    assert surface == "1:NW recovery-cascade-post-close-race"
    assert (
        app_env.main._golden_throne_tts_text("recovery-cascade-post-close-race", surface)
        == "Golden Throne resuming 1:NW recovery-cascade-post-close-race"
    )
    assert (
        app_env.main._golden_throne_banner_text("recovery-cascade-post-close-race", surface)
        == "GT resume: 1:NW recovery-cascade-post-close-race"
    )
