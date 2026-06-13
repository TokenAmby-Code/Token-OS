"""Discord error-log routing resolves a LIVE target at fire time.

Pins the corrected contract for `/api/discord/fixer-target`:

  * Default target is the live Fabricator General.
  * FG owns a config knob (``redirect``) to send error logs to one of its named
    LIVE children instead.
  * A stopped session-stub is NEVER returned — that was the live failure mode
    (routing to dead stub 019e64e4, then a dead fallback pane that spammed the
    Custodes pane).
"""

from __future__ import annotations

import sqlite3
from typing import Any

FG_LABEL = "mechanicus:fabricator-general"


def _insert_instance(
    db_path,
    instance_id,
    *,
    pane=None,
    parent=None,
    status="idle",
    pane_label=None,
    tab_name=None,
):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            profile_name, tts_voice, notification_sound, status, tmux_pane,
            parent_instance_id, pane_label)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', 'p', 'v', 's', ?, ?, ?, ?)""",
        (
            instance_id,
            f"{instance_id}-session",
            tab_name or instance_id,
            "/tmp",
            status,
            pane,
            parent,
            pane_label,
        ),
    )
    conn.commit()
    conn.close()


async def test_resolves_live_fg_by_default(app_env: Any) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "fg-live", pane="%33", pane_label=FG_LABEL)

    result = await main._resolve_discord_fixer_target()

    assert result["target"]["instance_id"] == "fg-live"
    assert result["target"]["tmux_pane"] == "%33"
    assert result["target"]["source"] == "fg"
    assert result["reason"] is None


async def test_no_live_fg_returns_none(app_env: Any) -> None:
    main = app_env.main
    # Only a non-FG instance is live.
    _insert_instance(app_env.db_path, "somebody", pane="%12", pane_label="legion:1")

    result = await main._resolve_discord_fixer_target()

    assert result["target"] is None
    assert result["reason"] == "no_live_fabricator_general"


async def test_stopped_fg_stub_is_never_targeted(app_env: Any) -> None:
    main = app_env.main
    # The exact disease: a stopped session-stub carrying the FG label. It must
    # not be returned even though its label matches.
    _insert_instance(
        app_env.db_path, "fg-stub-dead", pane="%29", pane_label=FG_LABEL, status="stopped"
    )

    result = await main._resolve_discord_fixer_target()

    assert result["target"] is None
    assert result["reason"] == "no_live_fabricator_general"


async def test_live_fg_preferred_over_stopped_stub(app_env: Any) -> None:
    main = app_env.main
    _insert_instance(
        app_env.db_path, "fg-stub-dead", pane="%29", pane_label=FG_LABEL, status="stopped"
    )
    _insert_instance(app_env.db_path, "fg-live", pane="%33", pane_label=FG_LABEL)

    result = await main._resolve_discord_fixer_target()

    assert result["target"]["instance_id"] == "fg-live"
    assert result["target"]["source"] == "fg"


async def test_redirect_routes_to_live_child(app_env: Any) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "fg-live", pane="%33", pane_label=FG_LABEL)
    _insert_instance(
        app_env.db_path, "child-7", pane="%46", parent="fg-live", pane_label="mechanicus:7"
    )

    result = await main._resolve_discord_fixer_target(redirect="mechanicus:7")

    assert result["target"]["instance_id"] == "child-7"
    assert result["target"]["tmux_pane"] == "%46"
    assert result["target"]["source"] == "redirect_child"
    assert result["reason"] is None


async def test_redirect_to_non_child_falls_back_to_fg(app_env: Any) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "fg-live", pane="%33", pane_label=FG_LABEL)
    # A live instance with the right label but parented elsewhere — NOT an FG
    # child, so the redirect is not honored.
    _insert_instance(app_env.db_path, "other-owner", pane="%50", pane_label="legion:custodes")
    _insert_instance(
        app_env.db_path,
        "stray-7",
        pane="%46",
        parent="other-owner",
        pane_label="mechanicus:7",
    )

    result = await main._resolve_discord_fixer_target(redirect="mechanicus:7")

    assert result["target"]["instance_id"] == "fg-live"
    assert result["target"]["source"] == "fg"
    assert result["reason"] == "redirect_not_live_child"


async def test_redirect_to_dead_child_falls_back_to_fg(app_env: Any) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "fg-live", pane="%33", pane_label=FG_LABEL)
    _insert_instance(
        app_env.db_path,
        "child-dead",
        pane="%46",
        parent="fg-live",
        pane_label="mechanicus:7",
        status="stopped",
    )

    result = await main._resolve_discord_fixer_target(redirect="mechanicus:7")

    assert result["target"]["instance_id"] == "fg-live"
    assert result["reason"] == "redirect_not_live_child"


def test_endpoint_returns_live_target(app_env: Any) -> None:
    from fastapi.testclient import TestClient

    _insert_instance(app_env.db_path, "fg-live", pane="%33", pane_label=FG_LABEL)
    client = TestClient(app_env.main.app)

    resp = client.get("/api/discord/fixer-target")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["target"]["instance_id"] == "fg-live"
    assert data["target"]["source"] == "fg"
