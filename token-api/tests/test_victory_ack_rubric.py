"""Integration tests for victory-ack rubric behavior and diagnosability."""

import sqlite3
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def va_client(app_env, monkeypatch) -> TestClient:
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


def test_get_rubric_surfaces_derived_provenance(va_client, app_env, tmp_path: Path) -> None:
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


def test_get_rubric_404_for_unknown_doc(va_client) -> None:
    resp = va_client.get("/api/session-docs/999999/rubric")
    assert resp.status_code == 404


def test_victory_ack_409_explains_derived_unmet(va_client, app_env, tmp_path: Path) -> None:
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


def test_victory_ack_success_mirrors_archived_status_to_file(
    va_client, app_env, tmp_path: Path
) -> None:
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


def _seed_instance_doc(db_path: Path, doc_path: Path, *, tab_name: str | None, link: bool) -> int:
    """Insert an active doc and optionally a linked instance; return doc_id."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO session_documents (file_path, title, status) VALUES (?, ?, 'active')",
        (str(doc_path), "Victory Ack Test"),
    )
    doc_id = cur.lastrowid
    if link:
        conn.execute(
            """INSERT INTO claude_instances
               (id, session_id, tab_name, working_dir, origin_type, device_id,
                status, session_doc_id)
               VALUES ('inst-va', 'sess-va', ?, '/tmp', 'local', 'Mac-Mini',
                       'idle', ?)""",
            (tab_name, doc_id),
        )
    conn.commit()
    conn.close()
    return doc_id


def _no_discord(monkeypatch, main) -> None:
    """Stub the outbound Discord notify so the ack never sends a real message."""

    def fake_run(*_a, **_k):
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(main.subprocess, "run", fake_run)


def _doc_status(db_path: Path, doc_id: int) -> str | None:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status FROM session_documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_victory_ack_blocks_on_unnamed_instance(app_env, monkeypatch) -> None:
    main = app_env.main
    _no_discord(monkeypatch, main)
    doc = _write_doc(app_env.db_path.parent, "victory:\n  instance_named: false")
    doc_id = _seed_instance_doc(app_env.db_path, doc, tab_name="needs-name", link=True)

    with pytest.raises(HTTPException) as exc:
        await main._victory_ack_core(doc_id, "done", [])

    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["error"] == "rubric_incomplete"
    assert "instance_named" in detail["missing"]
    # The block is non-destructive: the doc stays active.
    assert _doc_status(app_env.db_path, doc_id) == "active"


@pytest.mark.asyncio
async def test_victory_ack_passes_after_instance_named(app_env, monkeypatch) -> None:
    main = app_env.main
    _no_discord(monkeypatch, main)
    doc = _write_doc(app_env.db_path.parent, "victory:\n  instance_named: false")
    doc_id = _seed_instance_doc(app_env.db_path, doc, tab_name="needs-name", link=True)

    # Blocked while unnamed.
    with pytest.raises(HTTPException):
        await main._victory_ack_core(doc_id, "done", [])

    # Agent self-names → criterion derives True → ack succeeds and archives.
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        "UPDATE claude_instances SET tab_name = 'shipped-the-feature' WHERE id = 'inst-va'"
    )
    conn.commit()
    conn.close()

    result = await main._victory_ack_core(doc_id, "done", [])
    assert result["victory"] is True and result["archived"] is True
    assert _doc_status(app_env.db_path, doc_id) == "archived"


@pytest.mark.asyncio
async def test_victory_ack_no_linked_instance_still_acks(app_env, monkeypatch) -> None:
    """No linked instance means instance_named derives True: no false block."""
    main = app_env.main
    _no_discord(monkeypatch, main)
    doc = _write_doc(app_env.db_path.parent, "victory:\n  instance_named: false")
    doc_id = _seed_instance_doc(app_env.db_path, doc, tab_name=None, link=False)

    result = await main._victory_ack_core(doc_id, "done", [])
    assert result["victory"] is True and result["archived"] is True
    assert _doc_status(app_env.db_path, doc_id) == "archived"


@pytest.mark.asyncio
async def test_victory_ack_blocks_on_null_named_instance(app_env, monkeypatch) -> None:
    """A linked instance with a NULL tab_name is unnamed, not absent."""
    main = app_env.main
    _no_discord(monkeypatch, main)
    doc = _write_doc(app_env.db_path.parent, "victory:\n  instance_named: false")
    doc_id = _seed_instance_doc(app_env.db_path, doc, tab_name=None, link=True)

    with pytest.raises(HTTPException) as exc:
        await main._victory_ack_core(doc_id, "done", [])

    assert exc.value.status_code == 409
    assert "instance_named" in exc.value.detail["missing"]
    assert _doc_status(app_env.db_path, doc_id) == "active"


@pytest.mark.asyncio
async def test_victory_ack_blocks_on_empty_named_instance(app_env, monkeypatch) -> None:
    """A linked instance with an empty tab_name is unnamed, not absent."""
    main = app_env.main
    _no_discord(monkeypatch, main)
    doc = _write_doc(app_env.db_path.parent, "victory:\n  instance_named: false")
    doc_id = _seed_instance_doc(app_env.db_path, doc, tab_name="", link=True)

    with pytest.raises(HTTPException) as exc:
        await main._victory_ack_core(doc_id, "done", [])

    assert exc.value.status_code == 409
    assert "instance_named" in exc.value.detail["missing"]
    assert _doc_status(app_env.db_path, doc_id) == "active"
