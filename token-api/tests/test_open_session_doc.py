"""Integration tests for POST /api/session-docs/{doc_id}/open.

The single "open session doc by id" endpoint: both the tmux `prefix + S` keybind
(via cli-tools/bin/open-session-doc) and the ops cockpit double-click funnel here.
The server shells out to the `obsidian` CLI — stubbed here so no real GUI open
happens — so these tests cover the pure logic: id->file_path resolution, the
vault-relative note path, the obsidian:// URI, and the error mappings.
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def osd_client(app_env, monkeypatch, tmp_path):
    main = app_env.main
    # Point the vault root at a temp dir so file_path -> vault-relative resolves
    # deterministically (the endpoint reads IMPERIUM_ENV at request time).
    vault_root = tmp_path / "Imperium-ENV"
    (vault_root / "Mars" / "Sessions").mkdir(parents=True)
    monkeypatch.setenv("IMPERIUM_ENV", str(vault_root))

    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", _fake_run)
    return SimpleNamespace(client=TestClient(main.app), vault_root=vault_root, calls=calls)


def _insert_doc(app_env, file_path: str, status: str = "active") -> int:
    conn = sqlite3.connect(app_env.db_path)
    cur = conn.execute(
        "INSERT INTO session_documents (file_path, status) VALUES (?, ?)",
        (file_path, status),
    )
    conn.commit()
    doc_id = cur.lastrowid
    conn.close()
    return doc_id


def test_open_404_for_unknown_doc(osd_client) -> None:
    resp = osd_client.client.post("/api/session-docs/999999/open")
    assert resp.status_code == 404


def test_open_invokes_obsidian_cli_and_returns_uri(osd_client, app_env) -> None:
    doc_path = osd_client.vault_root / "Mars" / "Sessions" / "my-session.md"
    doc_path.write_text("---\nstatus: active\n---\n# s\n", encoding="utf-8")
    doc_id = _insert_doc(app_env, str(doc_path))

    resp = osd_client.client.post(f"/api/session-docs/{doc_id}/open")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["opened"] is True
    assert body["doc_id"] == doc_id
    assert (
        body["obsidian_uri"] == "obsidian://open?vault=Imperium-ENV&file=Mars/Sessions/my-session"
    )

    # The full OpenDocResult contract — pinned because more than one client now
    # funnels through this one endpoint: the tmux `prefix + S` keybind, the fleet
    # double-click, AND the ops cockpit Session Pipeline row-select. Every key the
    # web/ops `OpenDocResult` type reads must be present and well-formed.
    assert set(body) >= {"doc_id", "title", "file_path", "obsidian_uri", "opened"}
    assert body["file_path"] == str(doc_path)
    assert "title" in body  # nullable, but the key must exist for the typed client

    # The one open path shells out to the obsidian CLI with cardinal args.
    assert len(osd_client.calls) == 1
    argv = osd_client.calls[0]
    assert argv[0].endswith("/cli-tools/bin/obsidian")
    assert argv[1] == "vault=Imperium-ENV"
    assert "open" in argv
    assert argv[-1] == "path=Mars/Sessions/my-session.md"


def test_open_502_when_obsidian_fails(osd_client, app_env, monkeypatch) -> None:
    main = app_env.main
    doc_path = osd_client.vault_root / "Mars" / "Sessions" / "fail.md"
    doc_path.write_text("# s\n", encoding="utf-8")
    doc_id = _insert_doc(app_env, str(doc_path))

    def _boom(args, **kwargs):
        return SimpleNamespace(
            returncode=1, stdout="", stderr="obsidian open not supported on linux"
        )

    monkeypatch.setattr(main.subprocess, "run", _boom)
    resp = osd_client.client.post(f"/api/session-docs/{doc_id}/open")
    assert resp.status_code == 502
    assert "obsidian" in resp.json()["detail"]


def test_open_422_when_doc_outside_vault(osd_client, app_env, tmp_path: Path) -> None:
    outside = tmp_path / "outside-the-vault.md"
    outside.write_text("# s\n", encoding="utf-8")
    doc_id = _insert_doc(app_env, str(outside))
    resp = osd_client.client.post(f"/api/session-docs/{doc_id}/open")
    assert resp.status_code == 422
