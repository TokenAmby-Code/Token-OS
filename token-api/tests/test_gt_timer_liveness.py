"""Tests for Golden Throne timer liveness (Bug 2 of the GT-harness plan).

The scheduler has no persistent jobstore, so a token-api restart mid-wait drops
pending GT date-jobs and the one-shot startup recovery runs only once — a dropped
timer could strand a session for >12h. These tests cover the two fixes:

  - the periodic sweep re-arms a GT timer that was lost (recovery is idempotent
    and safe to re-run), and
  - GET /api/golden-throne/timers surfaces armed/next_fire/overdue so liveness is
    auditable from outside the driven thread.
"""

import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def gt(app_env, monkeypatch) -> SimpleNamespace:
    main = app_env.main
    # Deterministic + hermetic: never defer on real quiet-hours, and resolve no
    # live pane (so schedule's @GT_FIRE push no-ops without a tmux server).
    monkeypatch.setattr(main.shared, "get_quiet_hours_status", lambda *a, **k: {"active": False})

    async def _pane_gone(_instance_id):
        return (None, None)

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _pane_gone)
    return app_env


def _write_doc(tmp_path: Path, frontmatter: str) -> Path:
    doc = tmp_path / f"doc-{uuid.uuid4().hex[:8]}.md"
    doc.write_text(f"---\n{frontmatter}\n---\n\n# session\n", encoding="utf-8")
    return doc


def _insert_gt(db_path: Path, *, doc_path: Path, status: str = "idle", zealotry: int = 4) -> str:
    iid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO session_documents (file_path, status) VALUES (?, 'active')",
        (str(doc_path),),
    )
    doc_id = cur.lastrowid
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            tmux_pane, status, instance_type, zealotry, session_doc_id,
            registered_at, last_activity)
           VALUES (?, ?, ?, '/tmp', 'local', 'Mac-Mini', '%10', ?, 'golden_throne', ?, ?, ?, ?)""",
        (iid, str(uuid.uuid4()), f"gt-{iid[:8]}", status, zealotry, doc_id, now, now),
    )
    conn.commit()
    conn.close()
    return iid


@pytest.mark.asyncio
async def test_sweep_recovery_rearms_lost_gt_timer(gt, tmp_path):
    main = gt.main
    doc = _write_doc(tmp_path, "victory:\n  a: true\n  b: false")  # incomplete -> schedulable
    iid = _insert_gt(gt.db_path, doc_path=doc, status="idle")
    job_id = f"golden-throne-{iid}"

    # Simulate a restart that dropped the in-memory timer. Idempotent + narrow:
    # only remove when present, so no broad except masks an unexpected error.
    if main.scheduler.get_job(job_id) is not None:
        main.scheduler.remove_job(job_id)
    assert main.scheduler.get_job(job_id) is None

    # The sweep's underlying recovery re-arms it.
    recovered = await main.recover_recent_stopped_golden_throne_timers()
    assert any(r["instance_id"] == iid for r in recovered)
    assert main.scheduler.get_job(job_id) is not None


@pytest.mark.asyncio
async def test_sweep_recovery_skips_acknowledged_doc(gt, tmp_path):
    main = gt.main
    # Archived/acked doc must never be re-armed.
    doc = _write_doc(
        tmp_path, "victory:\n  a: true\nvictory_acknowledged_at: '2026-05-31T00:00:00'"
    )
    iid = _insert_gt(gt.db_path, doc_path=doc, status="idle")
    # Mark the linked doc archived so it is excluded by the recovery query.
    conn = sqlite3.connect(gt.db_path)
    conn.execute(
        "UPDATE session_documents SET status = 'archived' WHERE id = "
        "(SELECT session_doc_id FROM legacy_instances WHERE id = ?)",
        (iid,),
    )
    conn.commit()
    conn.close()

    recovered = await main.recover_recent_stopped_golden_throne_timers()
    assert all(r["instance_id"] != iid for r in recovered)
    assert main.scheduler.get_job(f"golden-throne-{iid}") is None


def test_timers_endpoint_reports_armed_and_overdue(gt, tmp_path):
    main = gt.main
    client = TestClient(main.app)

    doc1 = _write_doc(tmp_path, "victory:\n  a: false")
    armed_iid = _insert_gt(gt.db_path, doc_path=doc1, status="idle")
    doc2 = _write_doc(tmp_path, "victory:\n  a: false")
    unarmed_iid = _insert_gt(gt.db_path, doc_path=doc2, status="idle")

    # Arm one directly; leave the other unarmed (the "lost timer" case).
    main.scheduler.add_job(
        lambda: None,
        main.DateTrigger(run_date=datetime.now() + timedelta(seconds=60)),
        id=f"golden-throne-{armed_iid}",
        replace_existing=True,
    )

    resp = client.get("/api/golden-throne/timers")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    by_id = {t["instance_id"]: t for t in body["timers"]}
    assert by_id[armed_iid]["armed"] is True
    assert by_id[armed_iid]["overdue"] is False
    assert by_id[unarmed_iid]["armed"] is False
    assert by_id[unarmed_iid]["overdue"] is True  # unarmed + pre-ack => overdue
    assert by_id[unarmed_iid]["expected_delay_seconds"] == main.ZEALOTRY_DELAY_MAP[4]

    assert body["unarmed"] >= 1
    assert body["overdue"] >= 1
    assert body["sweep_interval_seconds"] == main.GOLDEN_THRONE_SWEEP_INTERVAL_SECONDS


def test_timers_endpoint_excludes_archived_docs(gt, tmp_path):
    main = gt.main
    client = TestClient(main.app)
    doc = _write_doc(tmp_path, "victory:\n  a: true")
    iid = _insert_gt(gt.db_path, doc_path=doc, status="idle")
    conn = sqlite3.connect(gt.db_path)
    conn.execute(
        "UPDATE session_documents SET status = 'archived' WHERE id = "
        "(SELECT session_doc_id FROM legacy_instances WHERE id = ?)",
        (iid,),
    )
    conn.commit()
    conn.close()

    body = client.get("/api/golden-throne/timers").json()
    assert all(t["instance_id"] != iid for t in body["timers"])


def test_zealotry_delay_in_range_is_exact(app_env):
    f = app_env.main._zealotry_delay_seconds
    assert f(4) == 1800  # loosest
    assert f(7) == 600
    assert f(10) == 60  # tightest


def test_zealotry_delay_out_of_range_clamps_never_silent_loosest(app_env):
    f = app_env.main._zealotry_delay_seconds
    # The footgun: a >10 value must NOT silently resolve to the loosest (1800s).
    assert f(11) == 60  # clamps to tightest (10), not 1800
    assert f(99) == 60
    # A below-range value clamps to the loosest valid level (4), not a KeyError.
    assert f(0) == 1800
    assert f(3) == 1800
