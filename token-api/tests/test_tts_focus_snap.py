"""Acceptance tests for TTS Playback Focus Snap.

Ticket: Mars/Tasks/TTS Playback Focus Snap.md

Rule: TTS playback focus snap is opt-in. Direct hot TTS produced by hooks must
not steal tmux focus; items explicitly promoted/played from the pause queue may
snap because the operator's play action is the focus intent.

These tests pin the hook (`_snap_focus_to_speaker`), the zoom-dedup primitive
(`_focus_and_zoom_pane`), and the wiring into `tts_queue_worker`. They cover
every edge case the ticket enumerates:

- pane no longer exists -> skip snap, playback continues
- rapid successive explicit-play items from different panes -> snap on each transition
- direct hot TTS -> no snap by default
- promoted pause-queue items -> snap
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
    # ``tmux_pane`` is accepted for caller readability but no longer stored: the
    # snap resolves the pane live from the tmuxctl oracle (resolve_instance_pane),
    # which tests control via monkeypatch. Only the device/mode columns matter here.
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, tts_mode, registered_at, last_activity)
           VALUES (?, ?, ?, '/tmp/test', 'local', ?, ?, ?,
                   datetime('now'), datetime('now'))""",
        (iid, str(uuid.uuid4()), f"test-{iid[:8]}", device_id, status, tts_mode),
    )
    conn.commit()
    conn.close()
    return iid


class _FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _make_item(tts, instance_id, tmux_pane="palace:1", *, focus_on_playback=False):
    return tts.TTSQueueItem(
        instance_id=instance_id,
        message="speak to me",
        voice="Microsoft George",
        sound="",
        tab_name="t",
        tmux_pane=tmux_pane,
        focus_on_playback=focus_on_playback,
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

    async def fake_resolve(resolved_instance_id):
        assert resolved_instance_id == iid  # oracle keyed by instance id
        return ("%42", "palace:speaker")

    monkeypatch.setattr(tts, "resolve_instance_pane", fake_resolve)

    focused = {}

    async def fake_focus(pane_id):
        focused["pane_id"] = pane_id
        return {"focused": True, "actions": ["select", "zoom"]}

    monkeypatch.setattr(tts, "_focus_and_zoom_pane", fake_focus)

    # The talking snap now funnels through the shared focus+zoom+mark primitive,
    # so the speaking pane is also stamped @OPS_SELECTED (one expand mechanism).
    marked = {}
    monkeypatch.setattr(tts, "_set_ops_selected", lambda pane_id: marked.update(pane_id=pane_id))

    result = asyncio.run(tts._snap_focus_to_speaker(_make_item(tts, iid)))
    assert result["snapped"] is True
    assert focused["pane_id"] == "%42"
    assert marked["pane_id"] == "%42"


def test_snap_skips_when_pane_dead(app_env, monkeypatch):
    """Instance died between queue and playback (pane no longer resolves):
    skip the snap, never touch tmux focus, never raise."""
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path, tmux_pane="palace:1")

    _patch_local_machine(tts, monkeypatch)
    _patch_routing_local(tts, monkeypatch)

    async def fake_resolve(resolved_instance_id):
        return (None, None)  # pane gone

    monkeypatch.setattr(tts, "resolve_instance_pane", fake_resolve)

    called = {"focus": False}

    async def fake_focus(pane_id):
        called["focus"] = True
        return {"focused": True}

    monkeypatch.setattr(tts, "_focus_and_zoom_pane", fake_focus)

    result = asyncio.run(tts._snap_focus_to_speaker(_make_item(tts, iid)))
    assert result["snapped"] is False
    # The oracle collapses "pane no longer resolves" into no_pane (no stored
    # column to distinguish a dead pane from an absent one).
    assert result["reason"] == "no_pane"
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

    async def fake_resolve(resolved_instance_id):
        return ("%42", "palace:speaker")

    async def boom(pane_id):
        raise RuntimeError("tmux exploded")

    monkeypatch.setattr(tts, "resolve_instance_pane", fake_resolve)
    monkeypatch.setattr(tts, "_focus_zoom_and_mark", boom)

    result = asyncio.run(tts._snap_focus_to_speaker(_make_item(tts, iid)))
    assert result["snapped"] is False
    assert result["reason"] == "error"


# --------------------------------------------------------------------------
# Wiring: tts_queue_worker calls the snap on the None -> item transition
# --------------------------------------------------------------------------


def test_worker_snaps_explicit_queue_playback(app_env, monkeypatch) -> None:
    """Items explicitly played from the queue snap focus on playback start."""
    tts = _load_tts(app_env)

    snapped = []

    async def fake_snap(item):
        snapped.append(item.instance_id)
        return {"snapped": True}

    monkeypatch.setattr(tts, "TTS_AUTO_FOCUS_ENABLED", False)
    monkeypatch.setattr(tts, "_snap_focus_to_speaker", fake_snap)
    monkeypatch.setattr(tts, "_set_tts_state", lambda *a, **k: None)
    monkeypatch.setattr(tts, "play_sound", lambda *a, **k: {"success": True})
    monkeypatch.setattr(tts, "speak_tts", lambda *a, **k: {"success": True, "method": "test"})

    tts.hot_queue.clear()
    tts.hot_queue.append(_make_item(tts, "alpha", tmux_pane="palace:1", focus_on_playback=True))
    tts.hot_queue.append(
        _make_item(tts, "bravo", tmux_pane="legion:custodes", focus_on_playback=True)
    )

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


def test_worker_does_not_snap_direct_hot_tts_by_default(app_env, monkeypatch) -> None:
    """Direct-to-surface hot TTS does not yank tmux focus."""
    tts = _load_tts(app_env)

    snapped = []

    async def fake_snap(item):
        snapped.append(item.instance_id)
        return {"snapped": True}

    monkeypatch.setattr(tts, "TTS_AUTO_FOCUS_ENABLED", False)
    monkeypatch.setattr(tts, "_snap_focus_to_speaker", fake_snap)
    monkeypatch.setattr(tts, "_set_tts_state", lambda *a, **k: None)
    monkeypatch.setattr(tts, "play_sound", lambda *a, **k: {"success": True})
    monkeypatch.setattr(tts, "speak_tts", lambda *a, **k: {"success": True, "method": "test"})

    tts.hot_queue.clear()
    tts.hot_queue.append(_make_item(tts, "alpha", tmux_pane="palace:1", focus_on_playback=False))

    async def drive():
        task = asyncio.create_task(tts.tts_queue_worker())
        for _ in range(20):
            await asyncio.sleep(0.05)
            if tts.tts_current is None and not tts.hot_queue:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert snapped == []


def test_worker_snaps_promoted_pause_queue_items(app_env, monkeypatch) -> None:
    """Promoting backlog to hot is an explicit play action and should snap focus."""
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
    # This simulates an item that was originally pause-queued, then later moved
    # into hot_queue by promote/play-pane. The promote endpoint sets
    # focus_on_playback=True because the operator pressed play.
    tts.hot_queue.append(
        _make_item(tts, "custodes-backlog", tmux_pane="legion:custodes", focus_on_playback=True)
    )

    async def drive():
        task = asyncio.create_task(tts.tts_queue_worker())
        for _ in range(20):
            await asyncio.sleep(0.05)
            if tts.tts_current is None and not tts.hot_queue:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert snapped == ["custodes-backlog"]


# --------------------------------------------------------------------------
# Shared select+expand primitive: @OPS_SELECTED marker + _focus_zoom_and_mark
# --------------------------------------------------------------------------


def _target(cmd):
    """The `-t` target of a tmux set-option command."""
    return cmd[cmd.index("-t") + 1]


def test_set_ops_selected_clears_others_then_sets_target(app_env, monkeypatch):
    """Exactly one pane carries @OPS_SELECTED: clear it everywhere else, set the target."""
    tts = _load_tts(app_env)
    calls = []

    def fake_tmux(args, timeout=2):
        calls.append(list(args))
        if args[0] == "list-panes":
            # %5 and %9 are stale-marked; %7 (the target) is currently unmarked.
            return _FakeProc(stdout="%5 1\n%7 \n%9 1\n")
        return _FakeProc()

    monkeypatch.setattr(tts, "_tmux", fake_tmux)
    tts._set_ops_selected("%7")

    sets = [c for c in calls if c and c[0] == "set-option"]
    unsets = [c for c in sets if "-u" in c]
    marks = [c for c in sets if "-u" not in c]
    # cleared the two stale panes, never the target
    assert sorted(_target(c) for c in unsets) == ["%5", "%9"]
    # set exactly the target, to "1"
    assert [_target(c) for c in marks] == ["%7"]
    assert marks[0][-1] == "1"


def test_set_ops_selected_survives_tmux_failure(app_env, monkeypatch):
    """list-panes failing (no server) must not raise — the marker is cosmetic."""
    tts = _load_tts(app_env)
    monkeypatch.setattr(tts, "_tmux", lambda *a, **k: None)
    tts._set_ops_selected("%1")  # must not raise


def test_focus_zoom_and_mark_marks_after_focus(app_env, monkeypatch):
    """The shared primitive focuses+zooms first, then stamps the marker."""
    tts = _load_tts(app_env)
    order = []

    async def fake_focus(pane_id):
        order.append(("focus", pane_id))
        return {"focused": True, "actions": ["select", "zoom"]}

    monkeypatch.setattr(tts, "_focus_and_zoom_pane", fake_focus)
    monkeypatch.setattr(tts, "_set_ops_selected", lambda pane_id: order.append(("mark", pane_id)))

    result = asyncio.run(tts._focus_zoom_and_mark("%3"))
    assert result["focused"] is True
    assert order == [("focus", "%3"), ("mark", "%3")]


# --------------------------------------------------------------------------
# select_instance_pane — the manual focus-pane endpoint resolver (feature A)
# --------------------------------------------------------------------------


def _patch_focus_zoom_mark(tts, monkeypatch, record):
    async def fake(pane_id):
        record["pane_id"] = pane_id
        return {"focused": True, "actions": ["select", "zoom"]}

    monkeypatch.setattr(tts, "_focus_zoom_and_mark", fake)


def test_select_instance_pane_happy_path(app_env, monkeypatch):
    """Local instance with a live pane: select + zoom + mark, resolving via the
    pane-identity surface (no raw %NN hand-rolling)."""
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path, tmux_pane="palace:1")
    _patch_local_machine(tts, monkeypatch)

    async def fake_resolve(resolved_instance_id):
        assert resolved_instance_id == iid
        return ("%42", "palace:speaker")

    monkeypatch.setattr(tts, "resolve_instance_pane", fake_resolve)
    rec = {}
    _patch_focus_zoom_mark(tts, monkeypatch, rec)

    result = asyncio.run(tts.select_instance_pane(iid))
    assert result["snapped"] is True
    assert result["reason"] is None
    assert rec["pane_id"] == "%42"


def test_select_instance_pane_bypasses_voice_chat(app_env, monkeypatch):
    """A manual double-click focuses even in voice-chat mode — the operator
    explicitly asked for this pane (the gate is talking-snap-only)."""
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path, tts_mode="voice-chat", tmux_pane="palace:1")
    _patch_local_machine(tts, monkeypatch)

    async def fake_resolve(resolved_instance_id):
        return ("%42", "palace:speaker")

    monkeypatch.setattr(tts, "resolve_instance_pane", fake_resolve)
    rec = {}
    _patch_focus_zoom_mark(tts, monkeypatch, rec)

    result = asyncio.run(tts.select_instance_pane(iid))
    assert result["snapped"] is True
    assert rec["pane_id"] == "%42"


def test_select_instance_pane_no_instance_id(app_env, monkeypatch):
    tts = _load_tts(app_env)
    result = asyncio.run(tts.select_instance_pane(""))
    assert result["snapped"] is False
    assert result["reason"] == "no_instance"


def test_select_instance_pane_instance_gone(app_env, monkeypatch):
    tts = _load_tts(app_env)
    _patch_local_machine(tts, monkeypatch)
    result = asyncio.run(tts.select_instance_pane("does-not-exist"))
    assert result["snapped"] is False
    assert result["reason"] == "instance_gone"


def test_select_instance_pane_remote(app_env, monkeypatch):
    """Cross-machine: you cannot focus a remote tmux pane locally."""
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path, device_id="TokenPC")
    _patch_local_machine(tts, monkeypatch, name="Mac-Mini")
    result = asyncio.run(tts.select_instance_pane(iid))
    assert result["snapped"] is False
    assert result["reason"] == "remote_pane"


def test_select_instance_pane_no_pane(app_env, monkeypatch):
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path, tmux_pane=None)
    _patch_local_machine(tts, monkeypatch)
    result = asyncio.run(tts.select_instance_pane(iid))
    assert result["snapped"] is False
    assert result["reason"] == "no_pane"


def test_select_instance_pane_dead(app_env, monkeypatch):
    tts = _load_tts(app_env)
    iid = _insert_instance(app_env.db_path, tmux_pane="palace:1")
    _patch_local_machine(tts, monkeypatch)

    async def fake_resolve(resolved_instance_id):
        return (None, None)

    monkeypatch.setattr(tts, "resolve_instance_pane", fake_resolve)
    result = asyncio.run(tts.select_instance_pane(iid))
    assert result["snapped"] is False
    assert result["reason"] == "no_pane"
