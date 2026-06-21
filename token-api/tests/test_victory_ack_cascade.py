"""Cascade cleanup for session-doc victorious→archived."""

import sqlite3
import types
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from session_doc_helpers import read_frontmatter


def _write_doc(tmp_path: Path, worktrees: list[dict[str, Any]], *, status: str = "active") -> Path:
    lines = ["---", f"status: {status}", "worktrees:"]
    for wt in worktrees:
        lines.append(f"  - path: {wt['path']}")
        lines.append(f"    branch: {wt['branch']}")
        if wt.get("port") is not None:
            lines.append(f"    port: {wt['port']}")
        lines.append(f"    status: {wt.get('status', 'active')}")
        lines.append("    claimed_at: '2026-06-21T00:00:00'")
    lines.extend(["---", "", "# session", ""])
    doc = tmp_path / f"doc-{uuid.uuid4().hex[:8]}.md"
    doc.write_text("\n".join(lines), encoding="utf-8")
    return doc


def _insert_doc(app_env, doc_path: Path, *, status: str = "active") -> int:
    conn = sqlite3.connect(app_env.db_path)
    cur = conn.execute(
        "INSERT INTO session_documents (file_path, title, status) VALUES (?, 'Cascade Test', ?)",
        (str(doc_path), status),
    )
    conn.commit()
    doc_id = cur.lastrowid
    conn.close()
    return int(doc_id)


def _insert_instance(
    app_env,
    *,
    iid: str,
    doc_id: int,
    working_dir: str,
    status: str = "working",
    pr_state: str | None = "merged",
) -> None:
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO instances
           (id, name, working_dir, origin_type, device_id, status, session_doc_id, pr_state)
           VALUES (?, ?, ?, 'local', 'Mac-Mini', ?, ?, ?)""",
        (iid, iid, working_dir, status, doc_id, pr_state),
    )
    conn.commit()
    conn.close()


def _db_doc_status(app_env, doc_id: int) -> str:
    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute("SELECT status FROM session_documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    return row[0]


@pytest.fixture
def client(app_env, monkeypatch) -> TestClient:
    def fake_discord(*_a, **_k):
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(app_env.main.subprocess, "run", fake_discord)
    return TestClient(app_env.main.app)


def test_victory_ack_cascade_dry_run_emits_actions_and_mutates_nothing(
    client, app_env, tmp_path: Path
) -> None:
    wt_path = tmp_path / "wt-feature"
    doc = _write_doc(tmp_path, [{"path": str(wt_path), "branch": "feat/cleanup", "port": 5173}])
    doc_id = _insert_doc(app_env, doc)
    _insert_instance(app_env, iid="inst-dry", doc_id=doc_id, working_dir=str(wt_path))

    resp = client.post(
        f"/api/session-docs/{doc_id}/victory-ack",
        json={"reason": "done", "force": True, "cascade": True},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["archived"] is False
    plan = body["cascade"]
    assert plan["worktrees"][0]["branch"] == "feat/cleanup"
    assert plan["linked_instances"][0]["id"] == "inst-dry"
    assert plan["live_instances"][0]["id"] == "inst-dry"
    assert plan["actions"] == [
        {
            "path": str(wt_path),
            "branch": "feat/cleanup",
            "port": 5173,
            "status": "active",
            "instances": [
                {
                    "id": "inst-dry",
                    "tab_name": "inst-dry",
                    "status": "working",
                    "working_dir": str(wt_path),
                    "pr_state": "merged",
                }
            ],
            "commands": [
                [
                    str(app_env.main.SCRIPTS_DIR / "cli-tools" / "bin" / "dev-server-stop"),
                    str(wt_path),
                ],
                [
                    str(app_env.main.SCRIPTS_DIR / "cli-tools" / "bin" / "worktree-delete"),
                    "feat/cleanup",
                    "-b",
                    "-f",
                    "--delete-remote",
                ],
            ],
        }
    ]
    assert _db_doc_status(app_env, doc_id) == "active"
    assert read_frontmatter(doc)[0]["status"] == "active"


@pytest.mark.asyncio
async def test_real_cascade_calls_worktree_delete_and_archives_doc(
    app_env, monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_run(args, **_kwargs):
        calls.append(tuple(args))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app_env.main, "_run_subprocess_offloop", fake_run)
    monkeypatch.setattr(
        app_env.main.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    wt_path = tmp_path / "wt-real"
    doc = _write_doc(tmp_path, [{"path": str(wt_path), "branch": "feat/real", "port": 8080}])
    doc_id = _insert_doc(app_env, doc)
    _insert_instance(app_env, iid="inst-real", doc_id=doc_id, working_dir=str(wt_path))

    result = await app_env.main._victory_ack_core(
        doc_id, "merged", [], force=True, cascade=True, dry_run=False
    )

    assert result["archived"] is True
    assert (
        str(app_env.main.SCRIPTS_DIR / "cli-tools" / "bin" / "dev-server-stop"),
        str(wt_path),
    ) in calls
    assert (
        str(app_env.main.SCRIPTS_DIR / "cli-tools" / "bin" / "worktree-delete"),
        "feat/real",
        "-b",
        "-f",
        "--delete-remote",
    ) in calls
    assert _db_doc_status(app_env, doc_id) == "archived"
    assert read_frontmatter(doc)[0]["status"] == "archived"


@pytest.mark.asyncio
async def test_real_cascade_is_idempotent_on_rerun(app_env, monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_run(args, **_kwargs):
        calls.append(tuple(args))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app_env.main, "_run_subprocess_offloop", fake_run)
    monkeypatch.setattr(
        app_env.main.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    wt_path = tmp_path / "wt-rerun"
    doc = _write_doc(
        tmp_path, [{"path": str(wt_path), "branch": "feat/rerun", "port": 3000}], status="archived"
    )
    doc_id = _insert_doc(app_env, doc, status="archived")
    _insert_instance(
        app_env, iid="inst-rerun", doc_id=doc_id, working_dir=str(wt_path), status="stopped"
    )

    result = await app_env.main._victory_ack_core(
        doc_id, "again", [], force=True, cascade=True, dry_run=False
    )

    assert result["archived"] is True
    assert calls == []
    assert _db_doc_status(app_env, doc_id) == "archived"


def test_open_pr_guard_warns_and_skips_worktree(client, app_env, tmp_path: Path) -> None:
    wt_path = tmp_path / "wt-open"
    doc = _write_doc(tmp_path, [{"path": str(wt_path), "branch": "feat/open", "port": 9000}])
    doc_id = _insert_doc(app_env, doc)
    _insert_instance(
        app_env, iid="inst-open", doc_id=doc_id, working_dir=str(wt_path), pr_state="open"
    )

    resp = client.post(
        f"/api/session-docs/{doc_id}/victory-ack",
        json={"reason": "done", "force": True, "cascade": True},
    )

    assert resp.status_code == 200, resp.text
    plan = resp.json()["cascade"]
    assert plan["actions"] == []
    assert plan["skipped"][0]["reason"] == "open_pr"
    assert "open PR" in plan["warnings"][0]


def test_self_guard_skips_operator_cwd_worktree(
    client, app_env, tmp_path: Path, monkeypatch
) -> None:
    doc = _write_doc(tmp_path, [{"path": str(tmp_path), "branch": "feat/self", "port": 7777}])
    doc_id = _insert_doc(app_env, doc)
    _insert_instance(app_env, iid="inst-self", doc_id=doc_id, working_dir=str(tmp_path))
    monkeypatch.chdir(tmp_path)

    resp = client.post(
        f"/api/session-docs/{doc_id}/victory-ack",
        json={"reason": "done", "force": True, "cascade": True},
    )

    assert resp.status_code == 200, resp.text
    plan = resp.json()["cascade"]
    assert plan["actions"] == []
    assert plan["skipped"][0]["reason"] == "self_guard"
