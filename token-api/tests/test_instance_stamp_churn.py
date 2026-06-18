"""SessionStart stamp/unstamp churn must not zero a still-live pane's @INSTANCE_ID.

Item #4 (sibling of PR #249). The same-ID ``--continue`` transplant branch of
``handle_session_start`` stamps the new pane then unstamps the old one:

    await _stamp_instance_id(tmux_pane, session_id, ...)
    if old_tmux_pane and old_tmux_pane != tmux_pane:
        await _unstamp_instance_id(old_tmux_pane, session_id)

The unstamp guard only checked ``old != new`` — NOT that ``tmux_pane`` is a real,
addressable pane. When an in-wrapper SessionStart re-fire arrives with no live
``TMUX_PANE`` (Claude Code strips it; a stall/SMB miss leaves it blank), the
stamp call no-ops (blank pane) while the unstamp still fires on ``old_tmux_pane``
— the pane the instance is STILL living on. ``_unstamp_instance_id`` sees the
stamp == this session_id (same instance!) and wipes it. The pane's @INSTANCE_ID
goes to zero even though nothing moved: the churn that drops resolve-instance to
0 and makes a leniently-armed pane fail strict assertion.

Fix: only unstamp the old pane when a NEW addressable pane was actually stamped
(``tmux_pane`` truthy and different) — mirroring the already-correct guard in the
sibling re-registration branch.

Hook-test doctrine: a FAKE pane id, mocked tmux writes, ``_unstamp_instance_id``
spied (never shelled). No live tmux is touched.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys

# A pane id that exists on NO live tmux server.
_LIVE_PANE = "%900917"
_NEW_PANE = "%900918"


def _insert(db_path, instance_id, *, pane=None, status="working", transplant_target=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            profile_name, tts_voice, notification_sound, status, tmux_pane)
           VALUES (?, ?, ?, '/tmp', 'local', 'Mac-Mini', 'p', 'v', 's', ?, ?)""",
        (instance_id, f"{instance_id}-session", instance_id, status, pane),
    )
    if transplant_target is not None:
        conn.execute(
            "UPDATE instances SET transplant_target_session = ? WHERE id = ?",
            (transplant_target, instance_id),
        )
    conn.commit()
    conn.close()


def _spy_stamp_writes(hooks, monkeypatch):
    """Mute every tmux WRITE; record (pane, session_id) the unstamp targets."""
    unstamped: list[tuple] = []

    async def _astamp(*_args, **_kwargs):
        return None

    async def _aunstamp(pane, session_id, *_a, **_k):
        unstamped.append((pane, session_id))

    def _sync_noop(*_args, **_kwargs):
        return None

    async def _no_label(_pane):
        return None

    async def _no_stamp(_pane):
        return None

    monkeypatch.setattr(hooks, "_stamp_instance_id", _astamp)
    monkeypatch.setattr(hooks, "_unstamp_instance_id", _aunstamp)
    monkeypatch.setattr(hooks, "_tmux_pane_label", _no_label)
    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", _no_stamp)
    monkeypatch.setattr(hooks.shared, "clear_pane_tint", _sync_noop)
    monkeypatch.setattr(hooks.shared, "apply_instance_pane_tint", _astamp)
    return unstamped


def _start(hooks, payload):
    return asyncio.run(hooks.handle_session_start(payload))


def test_blank_pane_refire_does_not_unstamp_live_pane(app_env, monkeypatch):
    """In-wrapper re-fire with no live TMUX_PANE must NOT wipe the live stamp.

    RED today: the unstamp fires on the still-occupied old pane (old != blank),
    zeroing @INSTANCE_ID on a pane that never moved.
    """
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "churn-1", pane=_LIVE_PANE, transplant_target="churn-1")
    unstamped = _spy_stamp_writes(hooks, monkeypatch)

    _start(
        hooks,
        {
            "session_id": "churn-1",
            "cwd": "/tmp",
            "pid": 4242,
            # No TMUX_PANE — the re-fire arrives with no addressable pane.
            "env": {"TOKEN_API_ENGINE": "claude"},
        },
    )

    assert unstamped == [], (
        f"a blank-pane re-fire must not unstamp the still-live pane: {unstamped}"
    )


def test_genuine_pane_move_still_unstamps_old_pane(app_env, monkeypatch):
    """Guard against over-correction: a real move to a NEW pane still vacates the
    old one's stamp so two panes never resolve to one UUID.
    """
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "move-1", pane=_LIVE_PANE, transplant_target="move-1")
    unstamped = _spy_stamp_writes(hooks, monkeypatch)

    _start(
        hooks,
        {
            "session_id": "move-1",
            "cwd": "/tmp",
            "pid": 4242,
            "env": {"TMUX_PANE": _NEW_PANE, "TOKEN_API_ENGINE": "claude"},
        },
    )

    assert (_LIVE_PANE, "move-1") in unstamped, (
        f"a genuine pane move must still unstamp the vacated pane: {unstamped}"
    )
