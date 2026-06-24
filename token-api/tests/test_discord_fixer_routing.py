"""Discord error-log routing resolves a LIVE target at fire time.

Pins the corrected contract for `/api/discord/fixer-target`:

  * Default target is the live Fabricator General.
  * FG owns a config knob (``redirect``) to send error logs to one of its named
    LIVE children instead.
  * A stopped session-stub is NEVER returned — that was the live failure mode
    (routing to dead stub 019e64e4, then a dead fallback pane that spammed the
    Custodes pane).

Pane geometry (pane id + label) is no longer a stored column; the resolver
sources it live from ``_live_agent_panes``. Tests control pane liveness by
patching that enumerator (the canonical ``_patch_panes`` pattern), seeding only
the registry row in the DB.
"""

from __future__ import annotations

import sqlite3
from typing import Any

FG_LABEL = "mechanicus:fabricator-general"


def _insert_instance(
    db_path,
    instance_id,
    *,
    parent=None,
    status="idle",
    tab_name=None,
):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            profile_name, tts_voice, notification_sound, status,
            parent_instance_id)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', 'p', 'v', 's', ?, ?)""",
        (
            instance_id,
            f"{instance_id}-session",
            tab_name or instance_id,
            "/tmp",
            status,
            parent,
        ),
    )
    conn.commit()
    conn.close()


def _pane(pane_id, instance_id, *, pane_label=None):
    return {
        "pane_id": pane_id,
        "pane_pid": 1234,
        "instance_id": instance_id,
        "pane_label": pane_label,
        "pane_role": pane_label,
        "current_command": "node",
    }


def _patch_panes(app_env, monkeypatch, panes):
    async def fake_panes():
        return list(panes)

    monkeypatch.setattr(app_env.main, "_live_agent_panes", fake_panes)


async def test_resolves_live_fg_by_default(app_env: Any, monkeypatch) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "fg-live")
    _patch_panes(app_env, monkeypatch, [_pane("%33", "fg-live", pane_label=FG_LABEL)])

    result = await main._resolve_discord_fixer_target()

    assert result["target"]["instance_id"] == "fg-live"
    assert result["target"]["tmux_pane"] == "%33"
    assert result["target"]["source"] == "fg"
    assert result["reason"] is None


async def test_no_live_fg_returns_none(app_env: Any, monkeypatch) -> None:
    main = app_env.main
    # Only a non-FG instance is live.
    _insert_instance(app_env.db_path, "somebody")
    _patch_panes(app_env, monkeypatch, [_pane("%12", "somebody", pane_label="mechanicus:1")])

    result = await main._resolve_discord_fixer_target()

    assert result["target"] is None
    assert result["reason"] == "no_live_fabricator_general"


async def test_stopped_fg_stub_is_never_targeted(app_env: Any, monkeypatch) -> None:
    main = app_env.main
    # The exact disease: a stopped session-stub carrying the FG label. It must
    # not be returned even though its label matches — and a stopped row owns no
    # live pane, so it never enters the routing set.
    _insert_instance(app_env.db_path, "fg-stub-dead", status="stopped")
    _patch_panes(app_env, monkeypatch, [])

    result = await main._resolve_discord_fixer_target()

    assert result["target"] is None
    assert result["reason"] == "no_live_fabricator_general"


async def test_live_fg_preferred_over_stopped_stub(app_env: Any, monkeypatch) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "fg-stub-dead", status="stopped")
    _insert_instance(app_env.db_path, "fg-live")
    _patch_panes(app_env, monkeypatch, [_pane("%33", "fg-live", pane_label=FG_LABEL)])

    result = await main._resolve_discord_fixer_target()

    assert result["target"]["instance_id"] == "fg-live"
    assert result["target"]["source"] == "fg"


async def test_redirect_routes_to_live_child(app_env: Any, monkeypatch) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "fg-live")
    _insert_instance(app_env.db_path, "child-7", parent="fg-live")
    _patch_panes(
        app_env,
        monkeypatch,
        [
            _pane("%33", "fg-live", pane_label=FG_LABEL),
            _pane("%46", "child-7", pane_label="mechanicus:7"),
        ],
    )

    result = await main._resolve_discord_fixer_target(redirect="mechanicus:7")

    assert result["target"]["instance_id"] == "child-7"
    assert result["target"]["tmux_pane"] == "%46"
    assert result["target"]["source"] == "redirect_child"
    assert result["reason"] is None


async def test_redirect_to_non_child_falls_back_to_fg(app_env: Any, monkeypatch) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "fg-live")
    # A live instance with the right label but parented elsewhere — NOT an FG
    # child, so the redirect is not honored.
    _insert_instance(app_env.db_path, "other-owner")
    _insert_instance(app_env.db_path, "stray-7", parent="other-owner")
    _patch_panes(
        app_env,
        monkeypatch,
        [
            _pane("%33", "fg-live", pane_label=FG_LABEL),
            _pane("%50", "other-owner", pane_label="council:custodes"),
            _pane("%46", "stray-7", pane_label="mechanicus:7"),
        ],
    )

    result = await main._resolve_discord_fixer_target(redirect="mechanicus:7")

    assert result["target"]["instance_id"] == "fg-live"
    assert result["target"]["source"] == "fg"
    assert result["reason"] == "redirect_not_live_child"


async def test_redirect_to_dead_child_falls_back_to_fg(app_env: Any, monkeypatch) -> None:
    main = app_env.main
    _insert_instance(app_env.db_path, "fg-live")
    # A stopped child owns no live pane, so the redirect is not honored.
    _insert_instance(app_env.db_path, "child-dead", parent="fg-live", status="stopped")
    _patch_panes(app_env, monkeypatch, [_pane("%33", "fg-live", pane_label=FG_LABEL)])

    result = await main._resolve_discord_fixer_target(redirect="mechanicus:7")

    assert result["target"]["instance_id"] == "fg-live"
    assert result["reason"] == "redirect_not_live_child"


def test_endpoint_returns_live_target(app_env: Any, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    _insert_instance(app_env.db_path, "fg-live")
    _patch_panes(app_env, monkeypatch, [_pane("%33", "fg-live", pane_label=FG_LABEL)])
    client = TestClient(app_env.main.app)

    resp = client.get("/api/discord/fixer-target")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["target"]["instance_id"] == "fg-live"
    assert data["target"]["source"] == "fg"
