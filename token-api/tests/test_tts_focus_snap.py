"""Acceptance tests for TTS Playback Focus Snap.

Ticket: Mars/Tasks/TTS Playback Focus Snap.md

Rule: when a TTS item begins *playback* (the moment the playback dispatcher
transitions `tts_current` from None -> item), snap the operator's tmux focus to
the originating pane and zoom it. No gate, no flag, no preference toggle.

These tests pin the hook (`_snap_focus_to_speaker`), the zoom-dedup primitive
(`_focus_and_zoom_pane`), and the wiring into `tts_queue_worker`. They cover
every edge case the ticket enumerates:

- pane no longer exists -> skip snap, playback continues
- rapid successive items from different panes -> snap on each transition
- custodes/cron-originated TTS with no real pane -> skip
- voice chat / Discord backend -> no snap
- cross-machine -> local-only snap (remote panes skipped)
- already zoomed on a different pane -> unzoom it first, then zoom speaker
- snap failure never propagates into the playback path
"""

import asyncio
import importlib
import sqlite3
import sys
import uuid
from pathlib import Path


def _load_tts(app_env):
    """Return the reloaded routes.tts module bound to the test DB."""
    token_api_dir = Path(__file__).resolve().parents[1]
    if str(token_api_dir) not in sys.path:
        sys.path.insert(0, str(token_api_dir))
    return sys.modules.get("routes.tts") or importlib.import_module("routes.tts")


def _insert_instance(
    db_path,
    *,
    instance_id=None,
    device_id="Mac-Mini",
    tmux_pane="palace:1",
    tts_mode="verbose",
    status="idle",
):
    iid = instance_id or str(uuid.uuid4())
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, tmux_pane, tts_mode, registered_at, last_activity)
           VALUES (?, ?, ?, '/tmp/test', 'local', ?, ?, ?, ?,
                   datetime('now'), datetime('now'))""",
        (iid, str(uuid.uuid4()), f"test-{iid[:8]}", device_id, status, tmux_pane, tts_mode),
    )
    conn.commit()
    conn.close()
    return iid


class _FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _make_item(tts, instance_id, tmux_pane="palace:1"):
    return tts.TTSQueueItem(
        instance_id=instance_id,
        message="speak to me",
        voice="Microsoft George",
        sound="",
        tab_name="t",
        tmux_pane=tmux_pane,
    )


def _patch_local_machine(tts, monkeypatch, name="Mac-Mini"):
    monkeypatch.setattr(tts, "_local_device_name", lambda: name)


def _patch_routing_local(tts, monkeypatch, device="wsl"):
    monkeypatch.setattr(
        tts, "resolve_tts_device", lambda *a, **k: {"device": device, "discord_bot": None}
    )


# --------------------------------------------------------------------------
# _focus_and_zoom_pane — the tmux primitive
# --------------------------------------------------------------------------


def test_focus_and_zoom_unzoomed_pane(app_env, monkeypatch):
    """Not zoomed: select the pane, then zoom it."""
    tts = _load_tts(app_env)
    calls = []

    def fake_tmux(args, timeout=2):
        calls.append(list(args))
        if args[0] == "list-panes":
            # active pane is the target, window not zoomed
            return _FakeProc(stdout="1 %5 0\n")
        return _FakeProc()

    monkeypatch.setattr(tts, "_tmux", fake_tmux)
    result = asyncio.run(tts._focus_and_zoom_pane("%5"))

    assert result["focused"] is True
    cmds = [c[0] for c in calls]
    assert "select-pane" in cmds
    # a single zoom toggle, targeting the speaker
    zooms = [c for c in calls if c[0] == "resize-pane" and "-Z" in c]
    assert len(zooms) == 1
    assert zooms[0][-1] == "%5"


def test_focus_and_zoom_already_zoomed_on_target(app_env, monkeypatch):
    """Already zoomed on the speaker: focus but do NOT toggle zoom off."""
    tts = _load_tts(app_env)
    calls = []

    def fake_tmux(args, timeout=2):
        calls.append(list(args))
        if args[0] == "list-panes":
            return _FakeProc(stdout="1 %5 1\n")  # target active AND zoomed
        return _FakeProc()

    monkeypatch.setattr(tts, "_tmux", fake_tmux)
    asyncio.run(tts._focus_and_zoom_pane("%5"))

    zooms = [c for c in calls if c[0] == "resize-pane" and "-Z" in c]
    assert zooms == [], "must not toggle zoom when already zoomed on the speaker"


def test_focus_and_zoom_different_pane_zoomed(app_env, monkeypatch):
    """A DIFFERENT pane is zoomed: unzoom it first, then zoom the speaker. No stacking."""
    tts = _load_tts(app_env)
    calls = []

    def fake_tmux(args, timeout=2):
        calls.append(list(args))
        if args[0] == "list-panes":
            # %9 is active+zoomed, %5 (our target) is not active
            return _FakeProc(stdout="1 %9 1\n0 %5 1\n")
        return _FakeProc()

    monkeypatch.setattr(tts, "_tmux", fake_tmux)
    asyncio.run(tts._focus_and_zoom_pane("%5"))

    zooms = [c for c in calls if c[0] == "resize-pane" and "-Z" in c]
    # exactly two toggles: unzoom the other pane, then zoom the speaker
    assert len(zooms) == 2
    assert zooms[0][-1] == "%9", "first unzoom the currently-zoomed pane"
    assert zooms[1][-1] == "%5", "then zoom the speaker"


# --------------------------------------------------------------------------
# _snap_focus_to_speaker — the hook orchestration + edge cases
# --------------------------------------------------------------------------


def test_snap_happy_path_local_pane(app_env, monkeypatch):
    """Local instance with a live pane: snap fires and resolves through the
    pane-identity surface (no raw %NN hand-rolling)."""
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path, tmux_pane="palace:1")

    _patch_local_machine(tts, monkeypatch)
    _patch_routing_local(tts, monkeypatch)

    async def fake_resolve(pane):
        assert pane == "palace:1"  # canonical surface value, not a raw %id
        return "%42"

    monkeypatch.setattr(tts, "resolve_tmux_pane_id", fake_resolve)

    focused = {}

    async def fake_focus(pane_id):
        focused["pane_id"] = pane_id
        return {"focused": True, "actions": ["select", "zoom"]}

    monkeypatch.setattr(tts, "_focus_and_zoom_pane", fake_focus)

    result = asyncio.run(tts._snap_focus_to_speaker(_make_item(tts, iid)))
    assert result["snapped"] is True
    assert focused["pane_id"] == "%42"


def test_snap_skips_when_pane_dead(app_env, monkeypatch):
    """Instance died between queue and playback (pane no longer resolves):
    skip the snap, never touch tmux focus, never raise."""
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path, tmux_pane="palace:1")

    _patch_local_machine(tts, monkeypatch)
    _patch_routing_local(tts, monkeypatch)

    async def fake_resolve(pane):
        return None  # pane gone

    monkeypatch.setattr(tts, "resolve_tmux_pane_id", fake_resolve)

    called = {"focus": False}

    async def fake_focus(pane_id):
        called["focus"] = True
        return {"focused": True}

    monkeypatch.setattr(tts, "_focus_and_zoom_pane", fake_focus)

    result = asyncio.run(tts._snap_focus_to_speaker(_make_item(tts, iid)))
    assert result["snapped"] is False
    assert result["reason"] == "pane_dead"
    assert called["focus"] is False


def test_snap_skips_when_instance_gone(app_env, monkeypatch):
    """No DB row at all (e.g. ephemeral/cron instance never registered): skip."""
    tts = _load_tts(app_env)
    _patch_local_machine(tts, monkeypatch)
    result = asyncio.run(tts._snap_focus_to_speaker(_make_item(tts, "does-not-exist")))
    assert result["snapped"] is False
    assert result["reason"] == "instance_gone"


def test_snap_skips_custodes_no_pane(app_env, monkeypatch):
    """Custodes/cron-originated TTS with no real tmux pane: skip snap."""
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path, tmux_pane=None)
    _patch_local_machine(tts, monkeypatch)
    _patch_routing_local(tts, monkeypatch)

    result = asyncio.run(tts._snap_focus_to_speaker(_make_item(tts, iid, tmux_pane=None)))
    assert result["snapped"] is False
    assert result["reason"] == "no_pane"


def test_snap_skips_voice_chat(app_env, monkeypatch):
    """Voice-chat mode: audio is a voice conversation, not tied to looking at
    the pane -> no snap."""
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path, tts_mode="voice-chat")
    _patch_local_machine(tts, monkeypatch)
    _patch_routing_local(tts, monkeypatch)

    result = asyncio.run(tts._snap_focus_to_speaker(_make_item(tts, iid)))
    assert result["snapped"] is False
    assert result["reason"] == "voice_chat"


def test_snap_skips_discord_backend(app_env, monkeypatch):
    """Discord voice backend: audio plays in the VC, not at a tmux pane -> no snap."""
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path)
    _patch_local_machine(tts, monkeypatch)
    _patch_routing_local(tts, monkeypatch, device="discord")

    result = asyncio.run(tts._snap_focus_to_speaker(_make_item(tts, iid)))
    assert result["snapped"] is False
    assert result["reason"] == "discord_backend"


def test_snap_skips_remote_pane(app_env, monkeypatch):
    """Cross-machine: pane owned by another machine -> local-only snap skips it."""
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path, device_id="TokenPC")
    _patch_local_machine(tts, monkeypatch, name="Mac-Mini")
    _patch_routing_local(tts, monkeypatch)

    result = asyncio.run(tts._snap_focus_to_speaker(_make_item(tts, iid)))
    assert result["snapped"] is False
    assert result["reason"] == "remote_pane"


def test_snap_never_raises_on_error(app_env, monkeypatch):
    """A snap miss must never fail the playback path. If the tmux primitive
    raises, the hook swallows it and returns a skip result."""
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path)
    _patch_local_machine(tts, monkeypatch)
    _patch_routing_local(tts, monkeypatch)

    async def fake_resolve(pane):
        return "%42"

    async def boom(pane_id):
        raise RuntimeError("tmux exploded")

    monkeypatch.setattr(tts, "resolve_tmux_pane_id", fake_resolve)
    monkeypatch.setattr(tts, "_focus_and_zoom_pane", boom)

    result = asyncio.run(tts._snap_focus_to_speaker(_make_item(tts, iid)))
    assert result["snapped"] is False
    assert result["reason"] == "error"


# --------------------------------------------------------------------------
# Wiring: tts_queue_worker calls the snap on the None -> item transition
# --------------------------------------------------------------------------


def test_worker_snaps_on_each_playback_start(app_env, monkeypatch):
    """The playback dispatcher snaps focus on every None -> item transition,
    including rapid successive items from different panes."""
    tts = _load_tts(app_env)

    snapped = []

    async def fake_snap(item):
        snapped.append(item.instance_id)
        return {"snapped": True}

    monkeypatch.setattr(tts, "_snap_focus_to_speaker", fake_snap)
    monkeypatch.setattr(tts, "_set_tts_state", lambda *a, **k: None)
    monkeypatch.setattr(tts, "play_sound", lambda *a, **k: {"success": True})
    monkeypatch.setattr(tts, "speak_tts", lambda *a, **k: {"success": True, "method": "test"})

    tts.hot_queue.clear()
    tts.hot_queue.append(_make_item(tts, "alpha", tmux_pane="palace:1"))
    tts.hot_queue.append(_make_item(tts, "bravo", tmux_pane="legion:custodes"))

    async def drive():
        task = asyncio.create_task(tts.tts_queue_worker())
        for _ in range(40):
            await asyncio.sleep(0.05)
            if len(snapped) >= 2:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert snapped[:2] == ["alpha", "bravo"]
