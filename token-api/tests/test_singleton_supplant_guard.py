"""A persona-less worker must never supplant a live SINGLETON's registry row.

Root cause (pinned live 2026-06-21): ``dispatch`` ran from inside an agent
inherited that agent's ``TOKEN_API_WRAPPER_LAUNCH_ID`` and injected it into the
worker. The worker's SessionStart then matched the dispatcher's row by that
shared id (routes/hooks.py branch 5) and *supplanted* it — re-keying the
operator's Custodes row onto the worker and, because the clean worker carries no
``launch_persona_id``, preserving ``persona_id=custodes``. The
``trg_instances_singleton_guard`` trigger then retired the real singleton. Across
the fleet this decapitated the dispatch-commander singletons dozens of times
(fabricator-general: 21 retired rows, custodes: 18).

``dispatch`` minting a fresh wrapper id (test_dispatch_cli) closes the origin,
but registration must also be hardened so ANY wrapper_launch_id / stamp collision
cannot transplant a live singleton's identity onto a registrant that does not
present that persona. A singleton's OWN in-wrapper re-fire (which DOES present
its persona via the pane label) must still adopt its row.

Companion to ``test_inwrapper_refire_adoption.py`` (the legit generic-worker
adoption path, which must stay green).
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys

# A pane id that does NOT exist on any live tmux server. The hooks' tmux-WRITE
# paths are mocked below too, but a fake pane is a second guard: never let a hook
# test touch a real live pane's @INSTANCE_ID.
_FAKE_PANE = "%999042"


def _seed_singleton(db_path, instance_id, persona_slug, *, wrapper_launch_id, status="working"):
    """Seed one live singleton-persona row carrying a wrapper_launch_id."""
    conn = sqlite3.connect(db_path)
    pid = conn.execute("SELECT id FROM personas WHERE slug = ?", (persona_slug,)).fetchone()[0]
    conn.execute(
        """INSERT INTO instances
             (id, name, engine, working_dir, device_id, origin_type,
              commander_type, status, persona_id, wrapper_launch_id)
           VALUES (?, ?, 'claude', '/tmp', 'Mac-Mini', 'local', 'emperor', ?, ?, ?)""",
        (instance_id, instance_id, status, pid, wrapper_launch_id),
    )
    conn.commit()
    conn.close()


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT i.id, i.status, p.slug AS persona_slug, p.default_rank "
        "FROM instances i LEFT JOIN personas p ON p.id = i.persona_id"
    ).fetchall()
    conn.close()
    return {r["id"]: r for r in rows}


def _blind_tmuxctl(hooks, monkeypatch):
    """No live pane resolution, no surviving stamp; mute every tmux write."""

    async def _none(_arg):
        return None

    async def _astamp(*_a, **_k):
        return None

    def _sync_noop(*_a, **_k):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", _none)
    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", _none)
    monkeypatch.setattr(hooks, "_stamp_instance_id", _astamp)
    monkeypatch.setattr(hooks, "_unstamp_instance_id", _astamp)
    monkeypatch.setattr(hooks.shared, "clear_pane_tint", _sync_noop)
    monkeypatch.setattr(hooks.shared, "apply_instance_pane_tint", _astamp)


def _start(hooks, payload):
    return asyncio.run(hooks.handle_session_start(payload))


def test_clean_worker_does_not_supplant_live_singleton_via_wrapper_id(app_env, monkeypatch):
    """The systemic bug: a persona-less worker sharing the operator's
    wrapper_launch_id must NOT supplant the operator's live Custodes row.

    RED today: branch-5 adoption re-keys the custodes row onto the worker and
    keeps persona_id=custodes, and the singleton guard then retires the original.
    """
    hooks = sys.modules["routes.hooks"]
    _seed_singleton(
        app_env.db_path,
        "operator-custodes",
        "custodes",
        wrapper_launch_id="SHARED-LAUNCH",
        status="working",
    )
    _blind_tmuxctl(hooks, monkeypatch)

    _start(
        hooks,
        {
            "session_id": "worker-new",
            "cwd": "/tmp",
            "pid": 7777,
            "wrapper_launch_id": "SHARED-LAUNCH",  # inherited/collided id
            # clean worker: no persona env, pane resolves to nothing
            "env": {
                "TMUX_PANE": _FAKE_PANE,
                "TOKEN_API_ENGINE": "claude",
                "TOKEN_API_LAUNCHER": "dispatch",
                "TOKEN_API_DISPATCH_TARGET": "mechanicus:new",
            },
        },
    )

    rows = _rows(app_env.db_path)
    # The operator's singleton row must survive intact — not re-keyed/decapitated.
    assert "operator-custodes" in rows, f"singleton row was decapitated: {dict(rows)}"
    assert rows["operator-custodes"]["persona_slug"] == "custodes"
    # The worker registers as its OWN fresh row...
    assert "worker-new" in rows, f"worker did not register fresh: {dict(rows)}"
    # ...and must NOT have inherited the singleton persona.
    assert rows["worker-new"]["persona_slug"] != "custodes", (
        f"worker leaked the singleton persona: {dict(rows['worker-new'])}"
    )


def test_singleton_own_refire_still_adopts_its_row(app_env, monkeypatch):
    """No false positive: a Custodes in-wrapper re-fire that DOES present its
    persona (via the pane label) still adopts its own row. Must stay green.
    """
    hooks = sys.modules["routes.hooks"]
    _seed_singleton(
        app_env.db_path,
        "custodes-old",
        "custodes",
        wrapper_launch_id="CUSTODES-LAUNCH",
        status="working",
    )
    _blind_tmuxctl(hooks, monkeypatch)

    _start(
        hooks,
        {
            "session_id": "custodes-new",
            "cwd": "/tmp",
            "pid": 8888,
            "wrapper_launch_id": "CUSTODES-LAUNCH",
            # the re-fire presents the custodes identity via its pane label
            "pane_label": hooks.CUSTODES_PANE_LABEL,
            "env": {"TMUX_PANE": _FAKE_PANE, "TOKEN_API_ENGINE": "claude"},
        },
    )

    rows = _rows(app_env.db_path)
    assert "custodes-new" in rows, f"singleton re-fire must adopt (re-key): {dict(rows)}"
    assert "custodes-old" not in rows, f"prior row must be adopted, not a zombie: {dict(rows)}"
    assert len(rows) == 1, f"singleton re-fire must not mint a duplicate: {dict(rows)}"
    assert rows["custodes-new"]["persona_slug"] == "custodes"
