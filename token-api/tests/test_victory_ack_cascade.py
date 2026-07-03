"""Cascade cleanup for session-doc victorious→archived."""

import importlib.machinery
import importlib.util
import json
import sqlite3
import types
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from session_doc_helpers import read_frontmatter


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def contains_exact(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return value == needle
    if isinstance(value, dict):
        return any(contains_exact(v, needle) for v in value.values())
    if isinstance(value, list):
        return any(contains_exact(v, needle) for v in value)
    return False


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
    from instance_mutation import insert_instance_sync

    now = datetime.now().isoformat()
    conn = sqlite3.connect(app_env.db_path)
    insert_instance_sync(
        conn,
        values={
            "id": iid,
            "name": iid,
            "engine": "codex",
            "working_dir": working_dir,
            "origin_type": "local",
            "device_id": "Mac-Mini",
            "status": status,
            "session_doc_id": doc_id,
            "pr_state": pr_state,
            "rank": "astartes",
            "created_at": now,
            "last_activity": now,
        },
        mutation_type="instance_registered",
        write_source="test",
        actor="test",
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
                    "tab_name": "needs-name",
                    "status": "working",
                    "working_dir": str(wt_path),
                    "pr_state": "merged",
                }
            ],
            "daemon_route": "/worktree/teardown",
            "daemon_request": {
                "worktree": str(wt_path),
                "branch": "feat/cleanup",
                "delete_remote": True,
                "instance_id": "inst-dry",
                "pr_state": "merged",
            },
            "commands": [],
            "peripherals": [
                {"name": "ghost-kill", "kind": "internal", "path": str(wt_path)},
                {
                    "name": "port-free",
                    "kind": "command",
                    "path": str(wt_path),
                    "command": [
                        "bash",
                        "-lc",
                        (
                            'source "$1"; '
                            'stop_port_process "$2" >/dev/null 2>&1 || true; '
                            'free_port "$2" >/dev/null 2>&1 || true; '
                            "prune_ports >/dev/null 2>&1 || true"
                        ),
                        "victory-cascade-port-free",
                        str(app_env.main.SCRIPTS_DIR / "cli-tools" / "lib" / "worktree-ports.sh"),
                        str(wt_path),
                    ],
                },
                {
                    "name": "session-doc-archive",
                    "kind": "session_doc_worktree_archive",
                    "doc_path": str(doc),
                    "path": str(wt_path),
                },
                {"name": "cd-alias-drop", "kind": "cd_alias", "branch": "feat/cleanup"},
            ],
        }
    ]
    flat = json_dumps(plan)
    assert "worktree-delete" not in flat
    assert not contains_exact(plan, "-f")
    assert "--delete-remote" not in flat
    assert _db_doc_status(app_env, doc_id) == "active"
    assert read_frontmatter(doc)[0]["status"] == "active"


@pytest.mark.asyncio
async def test_real_cascade_calls_worktree_delete_and_archives_doc(
    app_env, monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, ...]] = []
    daemon_calls: list[tuple[str, dict]] = []

    async def fake_run(args, **_kwargs):
        calls.append(tuple(args))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app_env.main, "_run_subprocess_offloop", fake_run)
    monkeypatch.setattr(app_env.main, "_kill_ghost_claude_for_worktree", lambda _path: [])
    monkeypatch.setattr(app_env.main, "_remove_cd_quick_alias", lambda _branch: True)

    def fake_daemon(route, body, **_kwargs):
        daemon_calls.append((route, dict(body)))
        return {"ok": True, "result": {"status": "removed", "reason": "merged"}}

    monkeypatch.setattr(app_env.shared, "_tmuxctld_post_json", fake_daemon)
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
    assert daemon_calls == [
        (
            "/worktree/teardown",
            {
                "worktree": str(wt_path),
                "branch": "feat/real",
                "delete_remote": True,
                "instance_id": "inst-real",
                "pr_state": "merged",
            },
        )
    ]
    assert calls and calls[0][0:3] == ("bash", "-lc", calls[0][2])
    assert "worktree-delete" not in json_dumps(result["cascade"])
    assert not contains_exact(result["cascade"], "-f")
    assert "--delete-remote" not in json_dumps(result["cascade"])
    assert [r["name"] for r in result["cascade_executed"][1:]] == [
        "ghost-kill",
        "port-free",
        "session-doc-archive",
        "cd-alias-drop",
    ]
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
        app_env.shared,
        "_tmuxctld_post_json",
        lambda *_a, **_k: {"ok": True, "result": {"status": "removed", "reason": "merged"}},
    )
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


def test_open_pr_routes_to_daemon_gate_instead_of_force_skipping(
    client, app_env, tmp_path: Path
) -> None:
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
    assert plan["actions"][0]["daemon_route"] == "/worktree/teardown"
    assert plan["actions"][0]["daemon_request"]["pr_state"] == "open"
    assert plan["skipped"] == []
    assert plan["warnings"] == []


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


def _load_victory_ack_cli():
    cli_path = Path(__file__).resolve().parents[2] / "cli-tools" / "bin" / "victory-ack"
    loader = importlib.machinery.SourceFileLoader("victory_ack_cli", str(cli_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("flag", ["--dry-run", "--execute"])
def test_victory_ack_cli_requires_cascade_for_cascade_mode_flags(flag: str) -> None:
    cli = _load_victory_ack_cli()

    with pytest.raises(SystemExit) as exc:
        cli.parse_args([flag, "done"])

    assert exc.value.code == "Error: --dry-run/--execute require --cascade."


@pytest.mark.asyncio
async def test_real_cascade_timeout_returns_structured_cleanup_failure(
    app_env, monkeypatch
) -> None:
    def fake_daemon(*_args, **_kwargs):
        return None

    monkeypatch.setattr(app_env.shared, "_tmuxctld_post_json", fake_daemon)
    plan = {
        "actions": [
            {
                "branch": "feat/timeout",
                "path": "/tmp/wt-timeout",
                "port": None,
                "daemon_route": "/worktree/teardown",
                "daemon_request": {"worktree": "/tmp/wt-timeout", "branch": "feat/timeout"},
                "peripherals": [],
            }
        ]
    }

    with pytest.raises(app_env.main.HTTPException) as exc:
        await app_env.main._execute_victory_cascade_plan(plan)

    assert exc.value.status_code == 500
    detail = exc.value.detail
    assert detail["error"] == "cascade_teardown_failed"
    assert detail["failed"]["route"] == "/worktree/teardown"
    assert detail["failed"]["returncode"] == 1
    assert detail["executed"] == [detail["failed"]]


@pytest.mark.asyncio
async def test_cascade_preserve_result_skips_all_peripherals(app_env, monkeypatch, tmp_path: Path):
    calls: list[tuple[str, ...]] = []

    async def fake_run(args, **_kwargs):
        calls.append(tuple(args))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app_env.main, "_run_subprocess_offloop", fake_run)
    monkeypatch.setattr(app_env.main, "_kill_ghost_claude_for_worktree", lambda _path: [123])
    monkeypatch.setattr(app_env.main, "_remove_cd_quick_alias", lambda _branch: True)
    monkeypatch.setattr(
        app_env.shared,
        "_tmuxctld_post_json",
        lambda *_a, **_k: {
            "ok": True,
            "result": {"status": "preserved", "reason": "branch_not_merged"},
        },
    )
    wt_path = tmp_path / "wt-preserved"
    doc = _write_doc(tmp_path, [{"path": str(wt_path), "branch": "feat/preserved", "port": 8081}])
    doc_id = _insert_doc(app_env, doc)
    _insert_instance(
        app_env,
        iid="inst-preserved",
        doc_id=doc_id,
        working_dir=str(wt_path),
        pr_state="open",
    )

    result = await app_env.main._victory_ack_core(
        doc_id, "preserve", [], force=True, cascade=True, dry_run=False
    )

    assert result["cascade_executed"] == [
        {
            "type": "daemon_teardown",
            "route": "/worktree/teardown",
            "request": {
                "worktree": str(wt_path),
                "branch": "feat/preserved",
                "delete_remote": True,
                "instance_id": "inst-preserved",
                "pr_state": "open",
            },
            "returncode": 0,
            "daemon_payload": {
                "ok": True,
                "result": {"status": "preserved", "reason": "branch_not_merged"},
            },
            "result": {"status": "preserved", "reason": "branch_not_merged"},
            "branch": "feat/preserved",
            "path": str(wt_path),
            "port": 8081,
        }
    ]
    assert calls == []
    fm = read_frontmatter(doc)[0]
    assert fm["worktrees"][0]["status"] == "active", "preserved worktree keeps doc entry"
