"""Tests for the universal WrapperEnd liveness guard (STRIKE D7a).

`handle_wrapper_end` correlates a terminal wrapper exit to an instance row by
`wrapper_launch_id` and marks it `stopped`. A spurious or duplicate WrapperEnd
once orphaned 4 live mechanicus rows + a live worker — it culled instances whose
tmux panes were still alive and working, on correlation alone with no liveness
check.

INVARIANT (universal — every live pane, not mechanicus-scoped): never mark an
instance `stopped` while its tmux pane is verifiably live. Before the status
update, `handle_wrapper_end` consults tmuxctld's runtime oracle
(`shared.resolve_instance_pane`, the @INSTANCE_ID stamp scan, fail-closed). A
live pane → REFUSE the stop, leave the row untouched, log
`wrapper_end_refused_live_pane`, return action `wrapper_end_refused_live`. A
dead/torn-down pane → the genuine exit passes through to `stopped` as before.

These drive the live `/api/hooks/WrapperEnd` endpoint and control the oracle's
verdict per-test by patching `shared.resolve_instance_pane`.
"""

import sqlite3
import uuid
from datetime import datetime

from fastapi.testclient import TestClient


def _insert(
    app_env, *, status="processing", wrapper_launch_id, instance_id=None, legion="mechanicus"
):
    """Insert a minimal instance and stamp its wrapper_launch_id on the durable row.

    The legacy_instances compatibility view does not carry wrapper_launch_id through
    its INSERT trigger, so set it directly on the instances table afterwards.
    """
    iid = instance_id or str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(str(app_env.db_path))
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, legion, synced, registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', ?, ?, 0, ?, ?)""",
        (iid, str(uuid.uuid4()), f"t-{iid[:8]}", "/tmp", status, legion, now, now),
    )
    conn.execute(
        "UPDATE instances SET wrapper_launch_id = ? WHERE id = ?",
        (wrapper_launch_id, iid),
    )
    conn.commit()
    conn.close()
    return iid


def _row(app_env, iid):
    conn = sqlite3.connect(str(app_env.db_path))
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM instances WHERE id = ?", (iid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def _set_status(app_env, iid, status, *, stopped_at=None, rank=None):
    conn = sqlite3.connect(str(app_env.db_path))
    if rank is not None:
        conn.execute("UPDATE instances SET rank = ? WHERE id = ?", (rank, iid))
    conn.execute(
        "UPDATE instances SET status = ?, stopped_at = ? WHERE id = ?",
        (status, stopped_at, iid),
    )
    conn.commit()
    conn.close()


def _event_types(app_env, iid):
    conn = sqlite3.connect(str(app_env.db_path))
    rows = conn.execute(
        "SELECT event_type FROM events WHERE instance_id = ? ORDER BY id", (iid,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _patch_live(app_env, monkeypatch, live_map):
    """Patch the oracle: live_map[instance_id] -> (pane_id, role); default dead."""

    async def fake_resolve(instance_id):
        return live_map.get(instance_id, (None, None))

    monkeypatch.setattr(app_env.shared, "resolve_instance_pane", fake_resolve)


def _post_wrapper_end(app_env, wrapper_launch_id, *, tmux_pane=""):
    payload = {"wrapper_launch_id": wrapper_launch_id, "engine": "claude"}
    if tmux_pane:
        payload["tmux_pane"] = tmux_pane
    return TestClient(app_env.main.app).post("/api/hooks/WrapperEnd", json=payload)


# ── (a) spurious/duplicate WrapperEnd vs a LIVE pane → refused ───────────────


def test_wrapper_end_refuses_when_pane_is_live(app_env, monkeypatch):
    """A live-stamped pane must survive a spurious WrapperEnd; row stays live."""
    wlid = "wlid-live-1"
    iid = _insert(app_env, status="processing", wrapper_launch_id=wlid)
    _patch_live(app_env, monkeypatch, {iid: ("%42", "mechanicus:fab")})

    resp = _post_wrapper_end(app_env, wlid)

    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "wrapper_end_refused_live", body
    assert body["instance_id"] == iid
    # The row must NOT have been culled.
    row = _row(app_env, iid)
    assert row["status"] == "working", "live pane was wrongly marked stopped"
    assert row["stopped_at"] is None
    assert "wrapper_end_refused_live_pane" in _event_types(app_env, iid)


def test_duplicate_wrapper_end_does_not_recull_live_worker(app_env, monkeypatch):
    """Repeated WrapperEnd against a live (non-mechanicus) worker is refused each time."""
    wlid = "wlid-live-dup"
    iid = _insert(app_env, status="processing", wrapper_launch_id=wlid, legion="astartes")
    _patch_live(app_env, monkeypatch, {iid: ("%7", "council:custodes")})

    for _ in range(3):
        resp = _post_wrapper_end(app_env, wlid)
        assert resp.json()["action"] == "wrapper_end_refused_live"

    assert _row(app_env, iid)["status"] == "working"


# ── (b) genuine WrapperEnd, pane gone/dead → marked stopped (preserved) ──────


def test_wrapper_end_stops_when_pane_dead(app_env, monkeypatch):
    """The oracle reports no live pane → genuine exit passes through to stopped."""
    wlid = "wlid-dead-1"
    iid = _insert(app_env, status="processing", wrapper_launch_id=wlid)
    _patch_live(app_env, monkeypatch, {})  # dead: resolve → (None, None)

    resp = _post_wrapper_end(app_env, wlid)

    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "wrapper_end_stopped_instance", body
    assert body["instance_id"] == iid
    row = _row(app_env, iid)
    assert row["status"] == "stopped", "genuine dead-pane exit should mark stopped"
    assert row["stopped_at"] is not None


def test_wrapper_end_idempotent_on_already_stopped(app_env, monkeypatch):
    """Already-stopped/archived/retired rows are left alone (existing behavior)."""
    wlid = "wlid-already"
    iid = _insert(app_env, status="processing", wrapper_launch_id=wlid)
    _set_status(app_env, iid, "stopped", stopped_at=datetime.now().isoformat())
    # Even if the oracle somehow reported live, a stopped row is not a candidate.
    _patch_live(app_env, monkeypatch, {iid: ("%9", None)})

    resp = _post_wrapper_end(app_env, wlid)

    body = resp.json()
    assert body["action"] == "wrapper_end_logged", body
    assert body["instance_id"] is None
    assert _row(app_env, iid)["status"] == "stopped"


def test_wrapper_end_leaves_retired_rank_alone(app_env, monkeypatch):
    """A retired-rank row is not a stop candidate regardless of liveness."""
    wlid = "wlid-retired"
    iid = _insert(app_env, status="processing", wrapper_launch_id=wlid)
    _set_status(app_env, iid, "stopped", stopped_at=datetime.now().isoformat(), rank="retired")
    _patch_live(app_env, monkeypatch, {})

    resp = _post_wrapper_end(app_env, wlid)

    assert resp.json()["action"] == "wrapper_end_logged"
    assert _row(app_env, iid)["rank"] == "retired"


# ── (c) revived worker's stale row reconciles to live without re-cull ────────


def test_revived_worker_stale_row_is_not_reculled(app_env, monkeypatch):
    """A row revived to live (stamp present) must not be culled by a late WrapperEnd.

    Models the post-revival reconcile window: the durable row reads live and its
    pane carries the @INSTANCE_ID stamp, so a trailing/stale WrapperEnd correlated
    by wrapper_launch_id must defer to the oracle and refuse, not re-cull.
    """
    wlid = "wlid-revived"
    iid = _insert(app_env, status="idle", wrapper_launch_id=wlid)
    _patch_live(app_env, monkeypatch, {iid: ("%21", "mechanicus:somnium")})

    resp = _post_wrapper_end(app_env, wlid)

    assert resp.json()["action"] == "wrapper_end_refused_live"
    row = _row(app_env, iid)
    assert row["status"] == "idle", "revived live row was re-culled"
    assert row["stopped_at"] is None
