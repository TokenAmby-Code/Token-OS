"""Pane-resurrection regressions surfaced by the FG recovery wave (2026-06-04).

R-H1 — persona pane-label singleton supplant. A persona pane (FG/Admin/Custodes)
is bound by its tmux pane label, and `primarch` is derived from that label via
PERSONA_PANE_IDENTITY. But when the label does not resolve at SessionStart time
(the @PANE_ID is not yet stamped on a fresh persona resume), no primarch is
derived, the primarch-singleton supplant (case 3) misses, and a *duplicate*
persona row is created while the prior row lingers un-demoted — leaving its stop
subscriptions orphaned (the live `1402f092`/`f55ac307`-at-`%96` split). The fix
supplants by the persona row already occupying this pane, independent of whether
the new registration's label resolved.

R-H2 — stop-subscription subscriber flag must resolve the *live pane* first. A
persona's instance id rotates on resume while it keeps its pane; a subscription
recorded against the now-dead id must still flag the live occupant of the pane.
Trusting `subscriber_instance_id` first flags a stopped row (silent no-op) and
the autonomous-wakeup marker never reaches the live subscriber.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys


def _insert(
    db_path,
    instance_id,
    *,
    pane=None,
    pane_label=None,
    primarch=None,
    legion="astartes",
    status="idle",
    hook_driven=0,
):
    # Pane geometry (tmux_pane/pane_label) is no longer stored — pane occupancy is
    # resolved live via ``instance_id_for_pane`` (monkeypatched per test). The ``pane``
    # / ``pane_label`` kwargs are accepted for call-site readability but not persisted.
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            profile_name, tts_voice, notification_sound, status,
            primarch, legion, hook_driven)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', 'p', 'v', 's', ?, ?, ?, ?)""",
        (
            instance_id,
            f"{instance_id}-session",
            instance_id,
            "/tmp",
            status,
            primarch,
            legion,
            hook_driven,
        ),
    )
    conn.commit()
    conn.close()


def _hook_driven(db_path, instance_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT hook_driven FROM legacy_instances WHERE id = ?", (instance_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _rows_with_primarch(db_path, primarch):
    # Pane ids are no longer stored, so pane occupancy can't be queried from the DB.
    # The supplant outcome surfaces instead through the persona/primarch carried by
    # the surviving row (supplant renames the prior row's id onto the new session,
    # preserving its persona_id → primarch projection).
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, primarch FROM legacy_instances WHERE primarch = ?", (primarch,)
    ).fetchall()
    conn.close()
    return rows


def _row_by_id(db_path, instance_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT id, primarch FROM legacy_instances WHERE id = ?", (instance_id,)
    ).fetchone()
    conn.close()
    return row


# ── R-H1: persona pane-label singleton supplant ────────────────────────────────


def test_persona_pane_supplant_when_label_unresolved(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]

    # Stale FG row from a prior (correctly-labelled) registration, now superseded.
    _insert(
        app_env.db_path,
        "stale-fg",
        pane="%fg",
        pane_label="mechanicus:fabricator-general",
        primarch="fabricator-general",
        legion="fabricator",
        status="stopped",
    )

    # The fresh persona resume cannot resolve its pane label yet → no primarch is
    # derived, so the primarch-singleton supplant (case 3) cannot fire.
    async def no_label(_pane):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)

    async def live_instance_for_pane(pane):
        return "stale-fg" if pane == "%fg" else None

    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", live_instance_for_pane)

    async def run():
        return await hooks.handle_session_start(
            {
                "session_id": "new-fg",
                "cwd": "/tmp",
                # no pid → the PID+pane supplant (case 4) cannot fire either
                "env": {"TMUX_PANE": "%fg", "TOKEN_API_ENGINE": "claude"},
            }
        )

    asyncio.run(run())

    rows = _rows_with_primarch(app_env.db_path, "fabricator-general")
    assert len(rows) == 1, f"expected supplant (1 row), got {len(rows)}: {rows}"
    surviving_id, surviving_primarch = rows[0]
    assert surviving_id == "new-fg", "the new session must take over the persona row"
    assert surviving_primarch == "fabricator-general", "persona identity preserved"


def test_non_persona_pane_is_not_supplanted(app_env, monkeypatch):
    # A recycled NON-persona pane (its prior occupant carried a non-persona primarch)
    # must NOT inherit the stale row's identity — the pane-label supplant is gated to
    # the known persona primarchs only. Registers clean as its own row. (CodeRabbit #83.)
    hooks = sys.modules["routes.hooks"]

    _insert(
        app_env.db_path,
        "stale-worker",
        pane="%wk",
        primarch="vulkan",  # non-persona primarch
        legion="astartes",
        status="stopped",
    )

    async def no_label(_pane):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)

    async def run():
        return await hooks.handle_session_start(
            {
                "session_id": "new-worker",
                "cwd": "/tmp",
                "env": {"TMUX_PANE": "%wk", "TOKEN_API_ENGINE": "claude"},
            }
        )

    asyncio.run(run())

    # No supplant: the stale non-persona row survives untouched and the new session
    # registers as its own distinct row. New registrations no longer persist tmux_pane,
    # so verify both rows by id rather than by pane.
    assert _row_by_id(app_env.db_path, "stale-worker") is not None, (
        "non-persona row must not be supplanted/migrated"
    )
    assert _row_by_id(app_env.db_path, "new-worker") is not None, "new session must register"


# ── R-H2: subscriber flag resolves the live pane over a dead id ─────────────────


def test_subscriber_flag_resolves_live_pane_over_dead_id(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]

    _insert(app_env.db_path, "watched-1", pane="%w")
    # Same pane, rotated identity: the subscription was recorded against the now
    # dead id, but the live occupant has a fresh id.
    _insert(app_env.db_path, "dead-sub", pane="%sub", status="stopped")
    _insert(app_env.db_path, "live-sub", pane="%sub", status="idle")

    async def fake_write(pane, payload):
        return {"status": "sent", "operation": "fake"}

    monkeypatch.setattr(hooks, "_direct_pane_write", fake_write)

    async def live_instance_for_pane(pane):
        return "live-sub" if pane == "%sub" else None

    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", live_instance_for_pane)

    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO stop_hook_subscriptions
           (target_instance_id, target_pane, subscriber_instance_id, subscriber_pane,
            event, delivery, status)
           VALUES ('watched-1', '%w', 'dead-sub', '%sub', 'stop', 'prompt', 'active')"""
    )
    conn.commit()
    conn.close()

    async def run():
        await hooks.handle_stop({"session_id": "watched-1"})

    asyncio.run(run())

    # The autonomous-wakeup marker must land on the LIVE pane occupant, not the
    # dead id named in the subscription.
    assert _hook_driven(app_env.db_path, "live-sub") == 1
    assert _hook_driven(app_env.db_path, "dead-sub") == 0


# ── Custodes reservation: a pane resolving to legion=custodes gets George ───────


def test_custodes_pane_assigns_reserved_george_profile(app_env, monkeypatch):
    # A fresh session in the council:custodes pane resolves to the custodes persona,
    # which must override whatever random chapter it drew at registration with the
    # reserved Custodes profile (George). George lives outside the rotation pools,
    # so this hook is the only path that ever assigns it.
    hooks = sys.modules["routes.hooks"]
    from shared import CUSTODES_PROFILE

    async def custodes_label(_pane):
        return hooks.CUSTODES_PANE_LABEL  # "council:custodes"

    monkeypatch.setattr(hooks, "_tmux_pane_label", custodes_label)

    async def run():
        return await hooks.handle_session_start(
            {
                "session_id": "cust-new",
                "cwd": "/tmp",
                "env": {"TMUX_PANE": "%cust", "TOKEN_API_ENGINE": "claude"},
            }
        )

    result = asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute(
        "SELECT legion, profile_name, tts_voice, notification_sound "
        "FROM legacy_instances WHERE id = ?",
        ("cust-new",),
    ).fetchone()
    conn.close()

    assert row is not None, "custodes session must register a row"
    legion, profile_name, tts_voice, notification_sound = row
    assert legion == "custodes"
    assert profile_name == CUSTODES_PROFILE["name"] == "custodes"
    assert tts_voice == CUSTODES_PROFILE["wsl_voice"] == "Microsoft George"
    assert notification_sound == CUSTODES_PROFILE["notification_sound"]

    # The SessionStart response carries display/chip/tint data only. Pane colour
    # is applied by tmux style; no Claude slash-color command is emitted.
    assert result["profile"] == "custodes"
    assert "cc_color" not in result
    assert result["color"] == CUSTODES_PROFILE["color"]
    assert result["chip_color"] == CUSTODES_PROFILE["chip_color"]
    assert result["pane_tint"] == CUSTODES_PROFILE["pane_tint"]


def test_voiceless_persona_pane_holds_no_voice(app_env, monkeypatch):
    # FG (and every persona except Custodes) must register with NO voice — it never
    # TTSes and never consumes a chapter voice slot. Its tmux-painted background
    # carries its identity; no Claude slash-color command is emitted.
    hooks = sys.modules["routes.hooks"]
    from shared import profile_by_name

    FABRICATOR_PROFILE = profile_by_name("fabricator-general")

    async def fg_label(_pane):
        return hooks.MECHANICUS_FG_LABEL  # "mechanicus:fabricator-general"

    monkeypatch.setattr(hooks, "_tmux_pane_label", fg_label)

    async def run():
        return await hooks.handle_session_start(
            {
                "session_id": "fg-new",
                "cwd": "/tmp",
                "env": {"TMUX_PANE": "%fg", "TOKEN_API_ENGINE": "claude"},
            }
        )

    result = asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute(
        "SELECT legion, profile_name, tts_voice FROM legacy_instances WHERE id = ?",
        ("fg-new",),
    ).fetchone()
    conn.close()

    assert row is not None, "FG session must register a row"
    legion, profile_name, tts_voice = row
    assert legion == "fabricator"
    assert profile_name == FABRICATOR_PROFILE["name"] == "fabricator-general"
    assert tts_voice is None, "FG must hold no voice (frees a chapter voice slot)"

    # Persona pane → no foreground slash-color; response carries tint data.
    assert result["profile"] == "fabricator-general"
    assert "cc_color" not in result
    assert result["pane_tint"] == FABRICATOR_PROFILE["pane_tint"]
