"""Tests for the live-panes / dead-rows registry fix.

The overnight `cleanup_stale_instances` sweep reaped registry rows for tmux panes
that were idle but still running Claude — collapsing the registry's active set to
~1 row while ~13 panes kept running ("live panes, dead rows"). This covers BOTH
halves of the fix:

* LIVENESS-GUARDED SWEEP — a row whose ``@INSTANCE_ID`` is stamped on a live agent
  pane is never reaped, no matter how long it has been idle.
* RE-REGISTRATION RECONCILER — a live agent pane whose stamp points at a
  stopped/archived row is a false-dead; reconcile reactivates the row and
  refreshes its tmux geometry, healing an already-collapsed active set.

The pane->instance bridge is the pane's ``@INSTANCE_ID`` stamp (the sweep only
touches the DB, never the pane, so a swept-but-live pane keeps its stamp). These
tests drive the enumerator with controlled pane lists rather than real tmux.
"""

import asyncio
import sqlite3
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace


def _insert(
    app_env,
    *,
    status="idle",
    last_activity=None,
    tmux_pane=None,
    legion="astartes",
    instance_id=None,
):
    """Insert a minimal instance via the legacy_instances compatibility view."""
    iid = instance_id or str(uuid.uuid4())
    now = last_activity or datetime.now().isoformat()
    conn = sqlite3.connect(str(app_env.db_path))
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, legion, synced, tmux_pane, registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', ?, ?, 0, ?, ?, ?)""",
        (iid, str(uuid.uuid4()), f"t-{iid[:8]}", "/tmp", status, legion, tmux_pane, now, now),
    )
    conn.commit()
    conn.close()
    return iid


def _insert_sub(app_env, *, target, subscriber, target_pane="%99", subscriber_pane="%10"):
    """Insert an active stop-hook subscription (watched=target, notify=subscriber)."""
    conn = sqlite3.connect(str(app_env.db_path))
    conn.execute(
        """INSERT INTO stop_hook_subscriptions
           (target_instance_id, target_pane, subscriber_instance_id, subscriber_pane,
            event, delivery, status, purpose)
           VALUES (?, ?, ?, ?, 'stop', 'prompt', 'active', 'generic')""",
        (target, target_pane, subscriber, subscriber_pane),
    )
    conn.commit()
    conn.close()


def _active_sub_count(app_env):
    conn = sqlite3.connect(str(app_env.db_path))
    n = conn.execute(
        "SELECT COUNT(*) FROM stop_hook_subscriptions WHERE status='active'"
    ).fetchone()[0]
    conn.close()
    return n


def _set_stopped_at(app_env, iid, when=None):
    conn = sqlite3.connect(str(app_env.db_path))
    conn.execute(
        "UPDATE instances SET stopped_at = ? WHERE id = ?",
        (when or datetime.now().isoformat(), iid),
    )
    conn.commit()
    conn.close()


def _row(app_env, iid):
    conn = sqlite3.connect(str(app_env.db_path))
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM instances WHERE id = ?", (iid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def _pane(pane_id, instance_id, *, pane_role=None, pane_label=None, pid=1234, cmd="node"):
    return {
        "pane_id": pane_id,
        "pane_pid": pid,
        "instance_id": instance_id,
        "pane_label": pane_label,
        "pane_role": pane_role,
        "current_command": cmd,
    }


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _patch_panes(app_env, monkeypatch, panes):
    async def fake_panes():
        return list(panes)

    monkeypatch.setattr(app_env.main, "_live_agent_panes", fake_panes)


OLD = None  # filled per-test from datetime.now()


# ── (a) liveness-guarded sweep ───────────────────────────────────────────────


def test_cleanup_spares_live_idle_pane(app_env, monkeypatch):
    """An idle-but-LIVE pane's row must NOT be swept; a dead idle row still is."""
    old = (datetime.now() - timedelta(hours=5)).isoformat()
    live = _insert(app_env, status="idle", last_activity=old)
    dead = _insert(app_env, status="idle", last_activity=old)
    _patch_panes(app_env, monkeypatch, [_pane("%16", live, pane_role="mechanicus:fab")])

    result = _run(app_env.main.cleanup_stale_instances())

    assert _row(app_env, live)["status"] == "idle", "live pane row was wrongly swept"
    assert _row(app_env, dead)["status"] == "stopped", "genuinely dead row should be reaped"
    assert result["protected_live"] >= 1
    assert result["cleaned_up"] >= 1


def test_cleanup_still_reaps_when_no_live_panes(app_env, monkeypatch):
    """With no live panes, the 3h idle reap is unchanged (regression guard)."""
    old = (datetime.now() - timedelta(hours=4)).isoformat()
    iid = _insert(app_env, status="idle", last_activity=old)
    _patch_panes(app_env, monkeypatch, [])

    result = _run(app_env.main.cleanup_stale_instances())

    assert _row(app_env, iid)["status"] == "stopped"
    assert result["cleaned_up"] >= 1


# ── (b) re-registration reconciler ───────────────────────────────────────────


def test_reconcile_reactivates_swept_live_row(app_env, monkeypatch):
    """A live pane whose stamp points at a stopped row → reactivate + refresh geometry."""
    swept = _insert(
        app_env,
        status="stopped",
        last_activity=(datetime.now() - timedelta(hours=5)).isoformat(),
    )
    _set_stopped_at(app_env, swept)
    _patch_panes(
        app_env,
        monkeypatch,
        [_pane("%21", swept, pane_role="mechanicus:somnium")],
    )

    result = _run(app_env.main.reconcile_live_panes())

    row = _row(app_env, swept)
    assert row["status"] == "idle", "swept-but-live row should be reactivated"
    assert row["stopped_at"] is None, "stopped_at must be cleared on reactivation"
    assert row["tmux_pane"] == "%21", "tmux_pane must be refreshed from the live pane"
    assert row["pane_label"] == "mechanicus:somnium", "pane_label must be refreshed"
    assert result["reactivated"] == 1


def test_reconcile_ignores_unmatched_and_unstamped(app_env, monkeypatch):
    """Unstamped panes and stamps with no DB row are left alone (no resurrection)."""
    stopped = _insert(app_env, status="stopped")
    _set_stopped_at(app_env, stopped)
    _patch_panes(
        app_env,
        monkeypatch,
        [
            _pane("%99", None),  # unstamped live pane → SessionStart owns cold reg
            _pane("%98", "ghost-id-not-in-db"),  # stamp with no row
        ],
    )

    result = _run(app_env.main.reconcile_live_panes())

    assert _row(app_env, stopped)["status"] == "stopped", "untouched stopped row resurrected"
    assert result["reactivated"] == 0
    assert result["unmatched"] >= 2


def test_reconcile_leaves_active_rows_active(app_env, monkeypatch):
    """An already-active row stamped on a live pane stays active (no spurious churn)."""
    active = _insert(app_env, status="idle")
    _patch_panes(app_env, monkeypatch, [_pane("%25", active, pane_role="legion:custodes")])

    result = _run(app_env.main.reconcile_live_panes())

    assert _row(app_env, active)["status"] == "idle"
    assert result["reactivated"] == 0


# ── (c) resolver detects Claude panes that surface as `node` ──────────────────


def test_live_agent_panes_detects_node_with_claude_child(app_env, monkeypatch):
    """Claude Code shows as `node`; a pane with a claude descendant counts as live."""

    async def fake_run(args, **kwargs):
        out = (
            "%16\t111\tinst-a\tWorker\tmechanicus:fab\tnode\n"  # node + claude child
            "%17\t222\tinst-b\t\t\tclaude\n"  # bare claude command
            "%18\t333\t\t\t\tbash\n"  # plain shell, no agent
        )
        return SimpleNamespace(returncode=0, stdout=out.encode())

    async def fake_tree():
        children = {111: [444], 222: [], 333: []}
        commands = {111: "node", 444: "claude --resume", 222: "claude", 333: "bash"}
        return children, commands

    monkeypatch.setattr(app_env.main, "_run_subprocess_offloop", fake_run)
    monkeypatch.setattr(app_env.main, "_ps_process_tree", fake_tree)

    panes = _run(app_env.main._live_agent_panes())
    by_id = {p["instance_id"] for p in panes}
    pane_ids = {p["pane_id"] for p in panes}

    assert "inst-a" in by_id, "node pane with a claude child must be detected"
    assert "inst-b" in by_id, "bare claude pane must be detected"
    assert "%18" not in pane_ids, "plain shell pane must be excluded"


def test_live_agent_instance_ids_derives_from_panes(app_env, monkeypatch):
    """The id set used by the sweep guard is exactly the stamped live panes."""
    _patch_panes(
        app_env,
        monkeypatch,
        [_pane("%1", "a"), _pane("%2", "b"), _pane("%3", None)],
    )
    ids = _run(app_env.main._live_agent_instance_ids())
    assert ids == {"a", "b"}


# ── (d) repro: overnight idle does NOT collapse the active set ────────────────


def test_overnight_idle_active_set_does_not_collapse(app_env, monkeypatch):
    """Many live panes idle past the cutoff → active set survives; only dead reaped."""
    old = (datetime.now() - timedelta(hours=6)).isoformat()
    live_ids = [_insert(app_env, status="idle", last_activity=old) for _ in range(5)]
    dead = _insert(app_env, status="idle", last_activity=old)
    _patch_panes(
        app_env,
        monkeypatch,
        [_pane(f"%{i + 16}", live_ids[i], pid=100 + i) for i in range(5)],
    )

    _run(app_env.main.cleanup_stale_instances())

    for iid in live_ids:
        assert _row(app_env, iid)["status"] == "idle", "live pane collapsed out of active set"
    assert _row(app_env, dead)["status"] == "stopped"


def test_reconcile_heals_fully_collapsed_state(app_env, monkeypatch):
    """The current production symptom: many stopped rows, all panes still live."""
    swept = []
    for _ in range(5):
        iid = _insert(app_env, status="stopped")
        _set_stopped_at(app_env, iid)
        swept.append(iid)
    _patch_panes(
        app_env,
        monkeypatch,
        [_pane(f"%{i + 16}", swept[i], pid=200 + i) for i in range(5)],
    )

    result = _run(app_env.main.reconcile_live_panes())

    assert result["reactivated"] == 5
    for iid in swept:
        assert _row(app_env, iid)["status"] == "idle"


# ── (e) sweep also GCs dead stop-hook subscriptions (Symptom 3) ───────────────


def test_cleanup_prunes_dead_hook_subscription(app_env, monkeypatch):
    """A subscription whose watched target is dead is GC'd as part of the sweep."""
    subscriber = _insert(app_env, status="idle", tmux_pane="%10")  # DB-live
    dead_target = _insert(app_env, status="stopped", tmux_pane=None)  # gone
    _set_stopped_at(app_env, dead_target)
    _insert_sub(app_env, target=dead_target, subscriber=subscriber)
    _patch_panes(app_env, monkeypatch, [])  # no live panes for the dead target

    result = _run(app_env.main.cleanup_stale_instances())

    assert _active_sub_count(app_env) == 0, "dead-target subscription must be pruned"
    assert result["pruned_hooks"] >= 1


def test_cleanup_spares_hook_for_swept_but_live_target(app_env, monkeypatch):
    """The critical guard: a stopped ROW whose pane is LIVE keeps its subscription.

    Without unioning the tmux liveness oracle into the prune's live set, a swept-
    but-live instance (the very Symptom-1 state) would have its still-valid hooks
    garbage-collected before the reconciler reactivates the row.
    """
    subscriber = _insert(app_env, status="idle", tmux_pane="%10")  # DB-live
    swept_live = _insert(app_env, status="stopped", tmux_pane=None)  # DB-dead...
    _set_stopped_at(app_env, swept_live)
    _insert_sub(app_env, target=swept_live, subscriber=subscriber)
    # ...but its pane is genuinely alive (tmux oracle), so it must be protected.
    _patch_panes(app_env, monkeypatch, [_pane("%21", swept_live)])

    result = _run(app_env.main.cleanup_stale_instances())

    assert _active_sub_count(app_env) == 1, "swept-but-live target's hook was wrongly pruned"
    assert result["pruned_hooks"] == 0


def test_cleanup_keeps_fully_live_hook(app_env, monkeypatch):
    """A subscription with both endpoints live survives the sweep untouched."""
    a = _insert(app_env, status="idle", tmux_pane="%14")
    b = _insert(app_env, status="idle", tmux_pane="%10")
    _insert_sub(app_env, target=a, target_pane="%14", subscriber=b)
    _patch_panes(app_env, monkeypatch, [])

    result = _run(app_env.main.cleanup_stale_instances())

    assert _active_sub_count(app_env) == 1
    assert result["pruned_hooks"] == 0
