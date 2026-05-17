import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient


class _FakeHTTPResponse:
    status_code = 500

    def json(self):
        return {}


class _FakeHTTPClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return _FakeHTTPResponse()


class _FakeProc:
    def __init__(
        self,
        returncode: int = 0,
        stdout: bytes = b"dispatched claude to legion:new",
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---")
    end = text.index("\n---", 3)
    return yaml.safe_load(text[3:end]) or {}


@pytest.fixture
def aspirant_env(app_env, tmp_path, monkeypatch):
    main = app_env.main
    vault = tmp_path / "Imperium-ENV"
    aspirants = vault / "Aspirants"
    sessions = vault / "Terra" / "Sessions"
    aspirants.mkdir(parents=True)
    sessions.mkdir(parents=True)

    monkeypatch.setattr(main, "OBSIDIAN_VAULT_PATH", vault)
    monkeypatch.setattr(main, "OBSIDIAN_INBOX_PATH", aspirants)
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeHTTPClient)

    return SimpleNamespace(main=main, vault=vault, aspirants=aspirants, sessions=sessions)


def test_inbox_create_launches_managed_legion_session(aspirant_env, monkeypatch):
    calls = []
    legacy_calls = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeProc()

    async def fake_run_implantation(*args, **kwargs):
        legacy_calls.append((args, kwargs))

    monkeypatch.setattr(
        aspirant_env.main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(aspirant_env.main, "run_implantation", fake_run_implantation)

    client = TestClient(aspirant_env.main.app)
    resp = client.post(
        "/api/inbox/create",
        json={
            "title": "Test Aspirant",
            "type": "capture",
            "content": "Build the new aspirant launch path.",
            "source": "pytest",
            "author": "tester",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True
    assert body["aspirant_session"]["launched"] is True
    assert legacy_calls == []

    note_path = aspirant_env.vault / body["path"]
    fm = _frontmatter(note_path)
    assert fm["aspirant_session_status"] == "launched"
    assert fm["aspirant_launcher"] == "dispatch"
    assert fm["aspirant_dispatch_target"] == "legion:new"
    assert Path(fm["aspirant_session_doc"]).exists()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:5] == (
        str(Path(aspirant_env.main.SCRIPTS_DIR) / "cli-tools" / "bin" / "dispatch"),
        "--target",
        "legion:new",
        "--dir",
        str(aspirant_env.vault),
    )
    assert "--session-doc" in args
    assert "--system-prompt-file" in args
    assert "--prompt-file" in args
    assert "--gt" in args
    assert kwargs["env"]["TOKEN_API_WRAPPER_LAUNCH_ID"] == fm["aspirant_launch_id"]

    prompt_file = Path(args[args.index("--prompt-file") + 1])
    system_file = Path(args[args.index("--system-prompt-file") + 1])
    assert "Build the new aspirant launch path." in prompt_file.read_text(encoding="utf-8")
    assert "full aspirant implantation/trials session" in system_file.read_text(encoding="utf-8")

    conn = sqlite3.connect(aspirant_env.main.DB_PATH)
    row = conn.execute(
        "SELECT title, file_path, project, status FROM session_documents WHERE id = ?",
        (int(fm["aspirant_session_doc_id"]),),
    ).fetchone()
    conn.close()
    assert row[0] == "Aspirant: Test Aspirant"
    assert Path(row[1]).name == "Aspirant - Test Aspirant.md"
    assert row[2] == "aspirants"
    assert row[3] == "active"


def test_inbox_notify_launches_existing_aspirant(aspirant_env, monkeypatch):
    calls = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeProc()

    monkeypatch.setattr(
        aspirant_env.main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    note = aspirant_env.aspirants / "Existing.md"
    note.write_text(
        """---
title: Existing
type: capture
status: inbox
---

> [!dna] Gene-Seed
> Existing aspirant intent.
""",
        encoding="utf-8",
    )

    client = TestClient(aspirant_env.main.app)
    resp = client.post(
        "/api/inbox/notify",
        json={
            "path": "Aspirants/Existing.md",
            "title": "Existing",
            "type": "capture",
            "source": "pytest",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["aspirant_session"]["launched"] is True
    assert len(calls) == 1
    assert _frontmatter(note)["aspirant_session_status"] == "launched"


def test_duplicate_inbox_notify_does_not_spawn_second_session(aspirant_env, monkeypatch):
    async def fail_create_subprocess_exec(*args, **kwargs):
        raise AssertionError("dispatch should not be called for duplicate notify")

    monkeypatch.setattr(
        aspirant_env.main.asyncio, "create_subprocess_exec", fail_create_subprocess_exec
    )

    note = aspirant_env.aspirants / "Duplicate.md"
    note.write_text(
        """---
title: Duplicate
type: capture
status: inbox
aspirant_launch_id: existing-launch
aspirant_session_status: launched
aspirant_session_doc: /tmp/existing.md
---

> [!dna] Gene-Seed
> Duplicate aspirant intent.
""",
        encoding="utf-8",
    )

    client = TestClient(aspirant_env.main.app)
    resp = client.post(
        "/api/inbox/notify",
        json={
            "path": "Aspirants/Duplicate.md",
            "title": "Duplicate",
            "type": "capture",
            "source": "pytest",
        },
    )

    assert resp.status_code == 200
    result = resp.json()["aspirant_session"]
    assert result["launched"] is False
    assert result["duplicate"] is True
    assert result["aspirant_launch_id"] == "existing-launch"


def test_dispatch_failure_records_failed_state(aspirant_env, monkeypatch):
    async def fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc(returncode=42, stderr=b"tmux unavailable")

    monkeypatch.setattr(
        aspirant_env.main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    note = aspirant_env.aspirants / "Failure.md"
    note.write_text(
        """---
title: Failure
type: capture
status: inbox
---

> [!dna] Gene-Seed
> Failure aspirant intent.
""",
        encoding="utf-8",
    )

    client = TestClient(aspirant_env.main.app)
    resp = client.post(
        "/api/inbox/notify",
        json={
            "path": "Aspirants/Failure.md",
            "title": "Failure",
            "type": "capture",
            "source": "pytest",
        },
    )

    assert resp.status_code == 500
    fm = _frontmatter(note)
    assert fm["aspirant_session_status"] == "failed"
    assert "tmux unavailable" in fm["aspirant_launch_error"]
