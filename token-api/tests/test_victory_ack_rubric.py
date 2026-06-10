"""Integration tests for the victory-ack rubric diagnosability fixes.

Covers the three Bug-1 fixes from the GT-harness plan:
  - GET /api/session-docs/{id}/rubric surfaces derived-field provenance +
    DB↔file status divergence (so a derived condition is never mistaken for a
    stale/cached read again).
  - victory-ack 409 explains *why* a derived condition is unmet.
  - a successful ack mirrors `status: archived` onto the file frontmatter so the
    DB and file agree (the divergence observed during the GT proof).
"""

import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def va_client(app_env, monkeypatch):
    main = app_env.main

    # _victory_ack_core shells out to the `discord` CLI on success — stub it so
    # the test never performs a real outward send.
    def _fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(main.subprocess, "run", _fake_run)
    return TestClient(main.app)


def _write_doc(tmp_path: Path, frontmatter: str) -> Path:
    doc = tmp_path / f"doc-{uuid.uuid4().hex[:8]}.md"
    doc.write_text(f"---\n{frontmatter}\n---\n\n# session\n", encoding="utf-8")
    return doc


def _insert_doc(app_env, doc_path: Path, status: str = "active") -> int:
    conn = sqlite3.connect(app_env.db_path)
    cur = conn.execute(
        "INSERT INTO session_documents (file_path, status) VALUES (?, ?)",
        (str(doc_path), status),
    )
    conn.commit()
    doc_id = cur.lastrowid
    conn.close()
    return doc_id


def test_get_rubric_surfaces_derived_provenance(va_client, app_env, tmp_path):
    doc = _write_doc(
        tmp_path,
        "status: active\n"
        "victory:\n"
        "  sanguinius_satisfied: true\n"  # literal true, but derived source isn't terminal
        "sanguinius_is: hovering at your shoulder",
    )
    doc_id = _insert_doc(app_env, doc)

    resp = va_client.get(f"/api/session-docs/{doc_id}/rubric")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ackable_without_force"] is False
    assert body["db_status"] == "active"
    assert body["file_status"] == "active"
    assert body["status_divergent"] is False
    sang = body["rubric"]["fields"]["sanguinius_satisfied"]
    assert sang["derived"] is True
    assert sang["value"] is False  # recomputed, literal ignored
    assert sang["derived_from"] == "sanguinius_is"
    assert sang["resolved_int"] == 2


def test_get_rubric_404_for_unknown_doc(va_client):
    resp = va_client.get("/api/session-docs/999999/rubric")
    assert resp.status_code == 404


def test_victory_ack_409_explains_derived_unmet(va_client, app_env, tmp_path):
    doc = _write_doc(
        tmp_path,
        "victory:\n  sanguinius_satisfied: true\nsanguinius_is: at the easel",
    )
    doc_id = _insert_doc(app_env, doc)

    resp = va_client.post(f"/api/session-docs/{doc_id}/victory-ack", json={"reason": "done"})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "rubric_incomplete"
    assert "sanguinius_satisfied" in detail["missing"]
    unmet = {u["field"]: u for u in detail["unmet"]}
    assert unmet["sanguinius_satisfied"]["derived"] is True
    # The message tells the operator it is derived and names the real source.
    assert "DERIVED" in detail["message"]
    assert "sanguinius_is" in detail["message"]


def test_victory_ack_success_mirrors_archived_status_to_file(va_client, app_env, tmp_path):
    # Terminal beautifier state -> rubric complete -> ack without force succeeds.
    doc = _write_doc(
        tmp_path,
        "status: active\n"
        "victory:\n"
        "  sanguinius_satisfied: false\n"  # literal false, but derived source IS terminal
        "sanguinius_is: folding my wings",
    )
    doc_id = _insert_doc(app_env, doc)

    resp = va_client.post(f"/api/session-docs/{doc_id}/victory-ack", json={"reason": "shipped"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived"] is True

    # DB status archived.
    conn = sqlite3.connect(app_env.db_path)
    db_status = conn.execute(
        "SELECT status FROM session_documents WHERE id = ?", (doc_id,)
    ).fetchone()[0]
    conn.close()
    assert db_status == "archived"

    # File status mirrored to archived (the DB↔file divergence fix), ack stamped.
    from session_doc_helpers import read_frontmatter

    fm, _ = read_frontmatter(doc)
    assert fm.get("status") == "archived"
    assert fm.get("victory_acknowledged_at")
    assert fm.get("victory_reason") == "shipped"

    # And the diagnostic now reports DB and file agree.
    body = va_client.get(f"/api/session-docs/{doc_id}/rubric").json()
    assert body["db_status"] == "archived"
    assert body["file_status"] == "archived"
    assert body["status_divergent"] is False
