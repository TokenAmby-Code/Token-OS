"""Engine-agnostic pushed statusline @-vars (unified-statusline-display).

The pane-border nametag is engine-agnostic: every field sources from the
engine-agnostic ``instances`` table via the existing ``pane_state_queue`` push
pipeline, so Claude and Codex panes light up IDENTICALLY (only
``instances.engine`` distinguishes them, and nothing in the resolvers branches on
it). These tests exercise the real ``shared`` helpers against a real SQLite
schema (no live tmux: the pushes only INSERT queue rows; the worker — out of
scope here — is what shells out to ``tmux set-option``):

  * ``@CWD``         — basename of ``working_dir`` (trailing-slash / root edges);
  * ``@PERSONA``     — ``personas.display_name`` from ``persona_id`` ("" when none);
  * ``@SESSION_DOC`` — ``session_documents.title`` from ``session_doc_id`` (rebind);
  * each push enqueues a ``(instance_id, '@VAR', value, tmux_pane)`` queue row.
"""

from __future__ import annotations

import asyncio
import sqlite3

import aiosqlite
import pytest

# ── seed helpers (direct schema writes; no live tmux) ──────────────────────────


def _seed_persona(db_path, persona_id: str, slug: str, display_name: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO personas (id, slug, display_name, default_rank) "
            "VALUES (?, ?, ?, 'astartes')",
            (persona_id, slug, display_name),
        )
        conn.commit()


def _seed_session_doc(db_path, file_path: str, title: str) -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO session_documents (file_path, title) VALUES (?, ?)",
            (file_path, title),
        )
        conn.commit()
        return int(cur.lastrowid)


def _insert_instance(
    db_path,
    instance_id: str,
    *,
    engine: str = "claude",
    persona_id: str | None = None,
    session_doc_id: int | None = None,
    working_dir: str | None = "/tmp/work",
    tmux_pane: str | None = "%5",
    name: str = "inst",
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO instances "
            "(id, name, device_id, engine, working_dir, persona_id, session_doc_id, tmux_pane) "
            "VALUES (?, ?, 'Mac-Mini', ?, ?, ?, ?, ?)",
            (instance_id, name, engine, working_dir, persona_id, session_doc_id, tmux_pane),
        )
        conn.commit()


def _set_session_doc(db_path, instance_id: str, session_doc_id: int | None) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE instances SET session_doc_id = ? WHERE id = ?",
            (session_doc_id, instance_id),
        )
        conn.commit()


def _queue_rows(db_path) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT instance_id, variable, value, tmux_pane FROM pane_state_queue ORDER BY id"
            ).fetchall()
        ]


def _push(shared, db_path, instance_id) -> dict:
    async def _run():
        async with aiosqlite.connect(db_path) as db:
            result = await shared.push_agnostic_pane_vars(db, instance_id)
            await db.commit()
            return result

    return asyncio.run(_run())


# ── @CWD ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "working_dir,expected",
    [
        ("/Users/x/worktrees/wt-foo", "wt-foo"),
        ("/Users/x/worktrees/wt-foo/", "wt-foo"),  # trailing slash
        ("/Users/x/worktrees/wt-foo///", "wt-foo"),  # multiple trailing slashes
        ("/", ""),  # root collapses to unset
        ("", ""),
        (None, ""),
        ("relative", "relative"),  # bare basename
    ],
)
def test_cwd_basename_edge_cases(app_env, working_dir, expected):
    assert app_env.shared.cwd_basename(working_dir) == expected


def test_cwd_var_is_basename_of_working_dir(app_env):
    shared, db_path = app_env.shared, app_env.db_path
    _insert_instance(db_path, "i-cwd", working_dir="/Users/x/worktrees/wt-foo/", tmux_pane="%9")
    values = _push(shared, db_path, "i-cwd")
    assert values["@CWD"] == "wt-foo"
    cwd_rows = [r for r in _queue_rows(db_path) if r[1] == "@CWD"]
    assert cwd_rows == [("i-cwd", "@CWD", "wt-foo", "%9")]


# ── @PERSONA (resolution + empty + engine-agnostic) ────────────────────────────


def test_persona_resolves_display_name(app_env):
    shared, db_path = app_env.shared, app_env.db_path
    _seed_persona(db_path, "p-vulkan", "statusline-vulkan", "Vulkan")
    _insert_instance(db_path, "i-persona", persona_id="p-vulkan")
    values = _push(shared, db_path, "i-persona")
    assert values["@PERSONA"] == "Vulkan"


def test_persona_empty_when_no_persona(app_env):
    shared, db_path = app_env.shared, app_env.db_path
    _insert_instance(db_path, "i-bare", persona_id=None)
    values = _push(shared, db_path, "i-bare")
    assert values["@PERSONA"] == ""
    persona_rows = [r for r in _queue_rows(db_path) if r[1] == "@PERSONA"]
    assert persona_rows == [("i-bare", "@PERSONA", "", "%5")]


def test_persona_resolution_is_engine_agnostic(app_env):
    """A Codex instance resolves the exact same @PERSONA as a Claude one."""
    shared, db_path = app_env.shared, app_env.db_path
    _seed_persona(db_path, "p-dorn", "statusline-dorn", "Dorn")
    _insert_instance(db_path, "i-claude", engine="claude", persona_id="p-dorn")
    _insert_instance(db_path, "i-codex", engine="codex", persona_id="p-dorn")
    claude = _push(shared, db_path, "i-claude")
    codex = _push(shared, db_path, "i-codex")
    assert claude["@PERSONA"] == codex["@PERSONA"] == "Dorn"


# ── @SESSION_DOC (resolution + rebind + unlinked) ──────────────────────────────


def test_session_doc_resolves_title(app_env):
    shared, db_path = app_env.shared, app_env.db_path
    doc = _seed_session_doc(db_path, "/vault/doc-a.md", "Unified Statusline")
    _insert_instance(db_path, "i-doc", session_doc_id=doc)
    values = _push(shared, db_path, "i-doc")
    assert values["@SESSION_DOC"] == "Unified Statusline"


def test_session_doc_empty_when_unlinked(app_env):
    shared, db_path = app_env.shared, app_env.db_path
    _insert_instance(db_path, "i-undoc", session_doc_id=None)
    values = _push(shared, db_path, "i-undoc")
    assert values["@SESSION_DOC"] == ""


def test_session_doc_updates_on_rebind(app_env):
    shared, db_path = app_env.shared, app_env.db_path
    doc_a = _seed_session_doc(db_path, "/vault/a.md", "Doc Alpha")
    doc_b = _seed_session_doc(db_path, "/vault/b.md", "Doc Beta")
    _insert_instance(db_path, "i-rebind", session_doc_id=doc_a)
    first = _push(shared, db_path, "i-rebind")
    assert first["@SESSION_DOC"] == "Doc Alpha"
    _set_session_doc(db_path, "i-rebind", doc_b)
    second = _push(shared, db_path, "i-rebind")
    assert second["@SESSION_DOC"] == "Doc Beta"


# ── queue mechanics ────────────────────────────────────────────────────────────


def test_push_enqueues_all_three_vars_with_pane(app_env):
    shared, db_path = app_env.shared, app_env.db_path
    _seed_persona(db_path, "p-sang", "statusline-sang", "Sanguinius")
    doc = _seed_session_doc(db_path, "/vault/sang.md", "Blood Angels Doc")
    _insert_instance(
        db_path,
        "i-all",
        persona_id="p-sang",
        session_doc_id=doc,
        working_dir="/tmp/wt-blood/",
        tmux_pane="%42",
    )
    _push(shared, db_path, "i-all")
    rows = _queue_rows(db_path)
    assert ("i-all", "@PERSONA", "Sanguinius", "%42") in rows
    assert ("i-all", "@SESSION_DOC", "Blood Angels Doc", "%42") in rows
    assert ("i-all", "@CWD", "wt-blood", "%42") in rows
    assert len(rows) == 3


def test_queue_pane_var_coerces_none_value_to_empty(app_env):
    shared, db_path = app_env.shared, app_env.db_path

    async def _run():
        async with aiosqlite.connect(db_path) as db:
            await shared.queue_pane_var(db, "i-x", "@PERSONA", None, "%1")
            await db.commit()

    asyncio.run(_run())
    assert _queue_rows(db_path) == [("i-x", "@PERSONA", "", "%1")]


def test_push_no_rows_for_unknown_instance(app_env):
    shared, db_path = app_env.shared, app_env.db_path
    values = _push(shared, db_path, "does-not-exist")
    assert values == {}
    assert _queue_rows(db_path) == []
