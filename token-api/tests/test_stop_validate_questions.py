import sqlite3
import uuid
from datetime import datetime

import pytest


@pytest.fixture
def client(app_env):
    from fastapi.testclient import TestClient

    return TestClient(app_env.main.app)


@pytest.fixture(autouse=True)
def _clear_pending(app_env):
    import routes.hooks as hooks

    hooks._self_eval_pending.clear()
    yield
    hooks._self_eval_pending.clear()


def _insert_instance(db_path, *, session_doc_id=None, is_subagent=0, victory_at=None):
    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, registered_at, last_activity, instance_type, workflow_state,
            stop_allowed, session_doc_id, is_subagent, victory_at)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', 'idle', ?, ?, 'golden_throne',
                   'worktree', 1, ?, ?, ?)""",
        (
            sid,
            str(uuid.uuid4()),
            f"test-{sid[:8]}",
            "/tmp",
            now,
            now,
            session_doc_id,
            is_subagent,
            victory_at,
        ),
    )
    conn.commit()
    conn.close()
    return sid


def _insert_doc(db_path, path):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """INSERT INTO session_documents (file_path, title, project, status, created_at, updated_at)
           VALUES (?, 'Test Doc', 'pytest', 'active', ?, ?)""",
        (str(path), datetime.now().isoformat(), datetime.now().isoformat()),
    )
    doc_id = cur.lastrowid
    conn.commit()
    conn.close()
    return doc_id


def _row(db_path, sid):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM claude_instances WHERE id = ?", (sid,)).fetchone()
    conn.close()
    return dict(row)


def _blocked_doc(tmp_path):
    p = tmp_path / "blocked.md"
    p.write_text(
        """---
questions:
  - question: What blocks safe stop?
    answer: null
    state: open
    importance: 10
---
body
""",
        encoding="utf-8",
    )
    return p


def test_stop_validate_blocks_golden_throne_with_unclosed_questions(app_env, client, tmp_path):
    doc_id = _insert_doc(app_env.db_path, _blocked_doc(tmp_path))
    sid = _insert_instance(app_env.db_path, session_doc_id=doc_id)

    resp = client.post("/api/hooks/StopValidate", json={"session_id": sid})
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "block"
    assert "What blocks safe stop?" in body["reason"]

    row = _row(app_env.db_path, sid)
    assert row["workflow_state"] == "blocked"
    assert row["workflow_blocked_reason"] == "questions_unclosed"
    assert row["stop_allowed"] == 0
    assert row["next_required_action"] == "self_eval"
    assert row["next_action_owner"] == "agent"


def test_second_stop_after_questions_self_eval_allowed(app_env, client, tmp_path):
    doc_id = _insert_doc(app_env.db_path, _blocked_doc(tmp_path))
    sid = _insert_instance(app_env.db_path, session_doc_id=doc_id)

    first = client.post("/api/hooks/StopValidate", json={"session_id": sid})
    assert first.json()["decision"] == "block"
    second = client.post("/api/hooks/StopValidate", json={"session_id": sid})
    assert second.status_code == 200
    assert second.json() == {}

    row = _row(app_env.db_path, sid)
    assert row["stop_allowed"] == 1
    assert row["workflow_blocked_reason"] is None


def test_no_questions_key_falls_through_to_existing_self_eval(app_env, client, tmp_path):
    p = tmp_path / "clear.md"
    p.write_text("---\ntitle: Clear\n---\nbody\n", encoding="utf-8")
    doc_id = _insert_doc(app_env.db_path, p)
    sid = _insert_instance(app_env.db_path, session_doc_id=doc_id)

    resp = client.post("/api/hooks/StopValidate", json={"session_id": sid})
    assert resp.status_code == 200
    assert resp.json()["decision"] == "block"
    assert _row(app_env.db_path, sid)["workflow_blocked_reason"] == "self_eval_required"


def test_no_block_when_subagent_or_victory(app_env, client, tmp_path):
    doc_id = _insert_doc(app_env.db_path, _blocked_doc(tmp_path))
    subagent_sid = _insert_instance(app_env.db_path, session_doc_id=doc_id, is_subagent=1)
    victory_sid = _insert_instance(
        app_env.db_path, session_doc_id=doc_id, victory_at=datetime.now().isoformat()
    )

    assert client.post("/api/hooks/StopValidate", json={"session_id": subagent_sid}).json() == {}
    assert client.post("/api/hooks/StopValidate", json={"session_id": victory_sid}).json() == {}
