"""Tests for the session-doc worktree registry (Phase 3).

POST /api/session-docs/{doc_id}/worktrees mutates the `worktrees:` list in a
session doc's frontmatter, holding the one-active invariant server-side. The
registry lives in frontmatter — the dormant `worktrees` DB table must stay empty.
"""

import sqlite3

import pytest
import yaml
from fastapi.testclient import TestClient


def _make_doc(tmp_path, db_path, *, title="WT Registry Test"):
    """Create a session-doc markdown file + its session_documents row; return (id, path)."""
    fp = tmp_path / "wt-registry-test.md"
    fp.write_text(
        f"---\ntitle: {title}\nstatus: active\nworktrees: []\n---\n\n# body\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO session_documents (file_path, title, status) VALUES (?, ?, 'active')",
        (str(fp), title),
    )
    conn.commit()
    doc_id = cur.lastrowid
    conn.close()
    return doc_id, fp


def _read_worktrees(fp):
    content = fp.read_text(encoding="utf-8")
    assert content.startswith("---")
    end = content.find("\n---", 3)
    fm = yaml.safe_load(content[3:end])
    return fm.get("worktrees") or []


@pytest.fixture
def client(app_env):
    return TestClient(app_env.main.app)


def _claim(client, doc_id, path, *, branch="b", port="8201", claimed_at="2026-06-02T00:00:00Z"):
    return client.post(
        f"/api/session-docs/{doc_id}/worktrees",
        json={
            "action": "claim",
            "path": path,
            "branch": branch,
            "port": port,
            "claimed_at": claimed_at,
        },
    )


# ── claim ────────────────────────────────────────────────────


def test_claim_adds_active_entry(client, app_env, tmp_path):
    doc_id, fp = _make_doc(tmp_path, app_env.db_path)
    resp = _claim(client, doc_id, "/wt/a", branch="feat-a", port="8201")
    assert resp.status_code == 200, resp.text

    wts = _read_worktrees(fp)
    assert len(wts) == 1
    e = wts[0]
    assert e["path"] == "/wt/a"
    assert e["branch"] == "feat-a"
    assert e["status"] == "active"
    assert str(e["port"]) == "8201"
    assert e["claimed_at"] == "2026-06-02T00:00:00Z"


def test_second_claim_demotes_prior_active(client, app_env, tmp_path):
    doc_id, fp = _make_doc(tmp_path, app_env.db_path)
    _claim(client, doc_id, "/wt/a", branch="feat-a")
    _claim(client, doc_id, "/wt/b", branch="feat-b")

    wts = _read_worktrees(fp)
    assert len(wts) == 2
    active = [w for w in wts if w["status"] == "active"]
    archived = [w for w in wts if w["status"] == "archived"]
    # Exactly one active, and it is the most recent claim.
    assert len(active) == 1
    assert active[0]["path"] == "/wt/b"
    assert len(archived) == 1
    assert archived[0]["path"] == "/wt/a"


def test_reclaiming_same_path_refreshes_in_place(client, app_env, tmp_path):
    doc_id, fp = _make_doc(tmp_path, app_env.db_path)
    _claim(client, doc_id, "/wt/a", branch="old", port="8201")
    _claim(client, doc_id, "/wt/a", branch="new", port="8202")

    wts = _read_worktrees(fp)
    assert len(wts) == 1  # no duplicate
    assert wts[0]["status"] == "active"
    assert wts[0]["branch"] == "new"
    assert str(wts[0]["port"]) == "8202"


# ── archive ──────────────────────────────────────────────────


def test_archive_flips_active_and_retains(client, app_env, tmp_path):
    doc_id, fp = _make_doc(tmp_path, app_env.db_path)
    _claim(client, doc_id, "/wt/a")

    resp = client.post(
        f"/api/session-docs/{doc_id}/worktrees",
        json={"action": "archive", "path": "/wt/a"},
    )
    assert resp.status_code == 200, resp.text

    wts = _read_worktrees(fp)
    assert len(wts) == 1  # retained, not deleted
    assert wts[0]["status"] == "archived"


# ── validation / 404 ─────────────────────────────────────────


def test_unknown_action_rejected(client, app_env, tmp_path):
    doc_id, _ = _make_doc(tmp_path, app_env.db_path)
    resp = client.post(
        f"/api/session-docs/{doc_id}/worktrees",
        json={"action": "bogus", "path": "/wt/a"},
    )
    assert resp.status_code == 400


def test_missing_path_rejected(client, app_env, tmp_path):
    doc_id, _ = _make_doc(tmp_path, app_env.db_path)
    resp = client.post(
        f"/api/session-docs/{doc_id}/worktrees",
        json={"action": "claim"},
    )
    assert resp.status_code == 400


def test_unknown_doc_404(client):
    resp = client.post(
        "/api/session-docs/999999/worktrees",
        json={"action": "claim", "path": "/wt/a"},
    )
    assert resp.status_code == 404


# ── DB worktrees table stays empty ───────────────────────────


def test_db_worktrees_table_untouched(client, app_env, tmp_path):
    doc_id, _ = _make_doc(tmp_path, app_env.db_path)
    _claim(client, doc_id, "/wt/a")
    _claim(client, doc_id, "/wt/b")

    conn = sqlite3.connect(app_env.db_path)
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='worktrees'"
        ).fetchone()
        if exists:
            count = conn.execute("SELECT COUNT(*) FROM worktrees").fetchone()[0]
            assert count == 0, "registry must live in frontmatter, not the DB worktrees table"
    finally:
        conn.close()
