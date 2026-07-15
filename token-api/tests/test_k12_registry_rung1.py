#!/usr/bin/env python3
"""k12-era registry rung 1 (R4 branch column + stamping + import) checks.

Covers: schema migration idempotency (fresh + legacy-shape DB), the
stamp_session_doc_branch helper (DB truth + frontmatter mirror, restamp
semantics), the worktree-claim frontmatter mirror (archive leaves branch
intact), the one-shot import script contract, and dispatch bash syntax.

Run directly: uv run --directory token-api python tests/test_k12_registry_rung1.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

import aiosqlite  # noqa: E402

import db_schema  # noqa: E402
from session_doc_helpers import (  # noqa: E402
    read_frontmatter,
    stamp_session_doc_branch,
    update_session_doc_worktrees,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILURES.append(name)


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="k12-rung1-"))


def _session_doc_columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(session_documents)")}
    finally:
        conn.close()


def _has_branch_index(db_path: Path) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        return bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_session_docs_branch'"
            ).fetchone()
        )
    finally:
        conn.close()


def test_migration_idempotency() -> None:
    d = _tmpdir()
    fresh = d / "fresh.db"
    db_schema.init_database_sync(fresh)
    db_schema.init_database_sync(fresh)  # second run must be a no-op, not an error
    cols = _session_doc_columns(fresh)
    check("fresh init has branch column", "branch" in cols, str(cols))
    check("fresh init has partial index", _has_branch_index(fresh))

    # Legacy-shape DB: session_documents without branch gains it via migration.
    legacy = d / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE session_documents ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " file_path TEXT NOT NULL UNIQUE, title TEXT, project TEXT,"
        " primarch_name TEXT, cron_job_id TEXT, status TEXT DEFAULT 'active',"
        " created_at TIMESTAMP, updated_at TIMESTAMP)"
    )
    conn.execute("INSERT INTO session_documents (file_path, title) VALUES ('/tmp/x.md', 'x')")
    conn.commit()
    conn.close()
    db_schema.init_database_sync(legacy)
    check("legacy DB gains branch column", "branch" in _session_doc_columns(legacy))
    check("legacy DB gains partial index", _has_branch_index(legacy))
    conn = sqlite3.connect(legacy)
    row = conn.execute("SELECT branch FROM session_documents WHERE title='x'").fetchone()
    conn.close()
    check("existing row stays honest-NULL", row is not None and row[0] is None, str(row))


def test_stamp_helper() -> None:
    d = _tmpdir()
    dbp = d / "agents.db"
    db_schema.init_database_sync(dbp)
    doc = d / "doc.md"
    doc.write_text("---\ntitle: t\nsession_doc_id: 1\n---\n\nbody line\n", encoding="utf-8")

    async def run() -> None:
        async with aiosqlite.connect(dbp) as db:
            await db.execute(
                "INSERT INTO session_documents (id, file_path, title) VALUES (1, ?, 't')",
                (str(doc),),
            )
            await stamp_session_doc_branch(db, 1, "lane-a", file_path=doc)
            await db.commit()
            row = await (
                await db.execute("SELECT branch FROM session_documents WHERE id=1")
            ).fetchone()
            check("stamp sets DB row", row is not None and row[0] == "lane-a", str(row))
        fm, body = read_frontmatter(doc)
        check("stamp mirrors frontmatter", fm.get("branch") == "lane-a", str(fm))
        check("stamp preserves body", "body line" in body)

        # Restamp-same: idempotent (row + mirror unchanged, no error).
        async with aiosqlite.connect(dbp) as db:
            await stamp_session_doc_branch(db, 1, "lane-a", file_path=doc)
            await db.commit()
        fm, _ = read_frontmatter(doc)
        check("restamp-same idempotent", fm.get("branch") == "lane-a", str(fm))

        # Restamp-new last-writes; file_path resolved from the row when omitted.
        async with aiosqlite.connect(dbp) as db:
            await stamp_session_doc_branch(db, 1, "lane-b")
            await db.commit()
            row = await (
                await db.execute("SELECT branch FROM session_documents WHERE id=1")
            ).fetchone()
            check("restamp-new last-writes DB", row is not None and row[0] == "lane-b", str(row))
        fm, _ = read_frontmatter(doc)
        check("restamp-new last-writes mirror", fm.get("branch") == "lane-b", str(fm))

        # Missing file: DB row still stamps, mirror failure is warn-don't-die.
        async with aiosqlite.connect(dbp) as db:
            await db.execute(
                "INSERT INTO session_documents (id, file_path, title) VALUES (2, '/nonexistent/gone.md', 'g')"
            )
            await stamp_session_doc_branch(db, 2, "lane-c")
            await db.commit()
            row = await (
                await db.execute("SELECT branch FROM session_documents WHERE id=2")
            ).fetchone()
            check(
                "missing file: DB stamp survives", row is not None and row[0] == "lane-c", str(row)
            )

    asyncio.run(run())


def test_claim_mirror() -> None:
    d = _tmpdir()
    doc = d / "doc.md"
    doc.write_text("---\ntitle: t\n---\n\nbody\n", encoding="utf-8")

    wts = update_session_doc_worktrees(
        doc, action="claim", path="/tmp/wt-x", branch="lane-x", claimed_at="2026-07-15"
    )
    fm, _ = read_frontmatter(doc)
    check("claim sets top-level branch", fm.get("branch") == "lane-x", str(fm))
    check(
        "claim sets array-entry branch",
        len(wts) == 1 and wts[0]["branch"] == "lane-x" and wts[0]["status"] == "active",
        str(wts),
    )

    # Re-claim onto a new branch: one-active invariant + top-level last-writes.
    wts = update_session_doc_worktrees(
        doc, action="claim", path="/tmp/wt-y", branch="lane-y", claimed_at="2026-07-15"
    )
    fm, _ = read_frontmatter(doc)
    check("re-claim last-writes top-level", fm.get("branch") == "lane-y", str(fm))
    active = [w for w in wts if w["status"] == "active"]
    check(
        "one-active invariant holds",
        len(active) == 1 and active[0]["path"] == "/tmp/wt-y",
        str(wts),
    )

    # Archive leaves top-level branch intact (last-known attribution truth).
    update_session_doc_worktrees(doc, action="archive", path="/tmp/wt-y")
    fm, _ = read_frontmatter(doc)
    check("archive leaves branch intact", fm.get("branch") == "lane-y", str(fm))
    check(
        "archive flips entry status",
        all(w["status"] == "archived" for w in fm.get("worktrees", [])),
        str(fm.get("worktrees")),
    )

    # Claim without a branch never writes a null top-level key.
    doc2 = d / "doc2.md"
    doc2.write_text("---\ntitle: u\n---\n\nbody\n", encoding="utf-8")
    update_session_doc_worktrees(doc2, action="claim", path="/tmp/wt-z", claimed_at="2026-07-15")
    fm, _ = read_frontmatter(doc2)
    check("branchless claim writes no top-level key", "branch" not in fm, str(fm))


def _run_import(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "import_session_documents.py"), *argv],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_import_script() -> None:
    d = _tmpdir()
    src = d / "src.db"
    conn = sqlite3.connect(src)
    conn.execute(
        "CREATE TABLE session_documents ("
        " id INTEGER PRIMARY KEY, file_path TEXT NOT NULL UNIQUE, title TEXT,"
        " project TEXT, status TEXT DEFAULT 'active', created_at TEXT, updated_at TEXT)"
    )
    olddocs = d / "olddocs"
    olddocs.mkdir()
    doc_active = olddocs / "a.md"
    doc_active.write_text(
        "---\ntitle: a\nworktrees:\n"
        "  - path: /x\n    branch: lane-old\n    status: archived\n"
        "  - path: /y\n    branch: lane-live\n    status: active\n---\nbody\n",
        encoding="utf-8",
    )
    doc_top = olddocs / "b.md"
    doc_top.write_text("---\ntitle: b\nbranch: top-branch\n---\nbody\n", encoding="utf-8")
    doc_plain = olddocs / "c.md"
    doc_plain.write_text("---\ntitle: c\n---\nbody\n", encoding="utf-8")
    conn.execute(
        "INSERT INTO session_documents (id, file_path, title) VALUES (7, ?, 'a')",
        (str(doc_active),),
    )
    conn.execute(
        "INSERT INTO session_documents (id, file_path, title) VALUES (9, ?, 'b')", (str(doc_top),)
    )
    conn.execute(
        "INSERT INTO session_documents (id, file_path, title) VALUES (11, ?, 'c')",
        (str(doc_plain),),
    )
    conn.execute(
        "INSERT INTO session_documents (id, file_path, title) VALUES (13, '/nonexistent/z.md', 'z')"
    )
    conn.commit()
    conn.close()

    # Remap olddocs -> newdocs to exercise --map-prefix (the missing-file row stays unmapped).
    newdocs = d / "newdocs"
    import shutil

    shutil.copytree(olddocs, newdocs)
    map_arg = f"{olddocs}={newdocs}"

    dest = d / "dest.db"
    db_schema.init_database_sync(dest)

    # Dry-run: full summary, zero writes.
    r = _run_import("--source", str(src), "--dest", str(dest), "--map-prefix", map_arg, "--dry-run")
    check("dry-run exits 0", r.returncode == 0, r.stderr)
    summary = json.loads(r.stdout) if r.returncode == 0 else {}
    check("dry-run flagged", summary.get("dry_run") is True, r.stdout)
    conn = sqlite3.connect(dest)
    count = conn.execute("SELECT count(*) FROM session_documents").fetchone()[0]
    conn.close()
    check("dry-run writes nothing", count == 0, str(count))

    # Real import.
    r = _run_import("--source", str(src), "--dest", str(dest), "--map-prefix", map_arg)
    check("import exits 0", r.returncode == 0, r.stderr)
    summary = json.loads(r.stdout) if r.returncode == 0 else {}
    check(
        "summary counts",
        summary.get("imported") == 4
        and summary.get("branch_stamped") == 2
        and summary.get("branch_null") == 2
        and summary.get("files_missing") == 1
        and summary.get("paths_unmapped") == 1,
        r.stdout,
    )
    conn = sqlite3.connect(dest)
    rows = conn.execute(
        "SELECT id, file_path, branch FROM session_documents ORDER BY id"
    ).fetchall()
    conn.close()
    check("ids preserved", [row[0] for row in rows] == [7, 9, 11, 13], str(rows))
    by_id = {row[0]: row for row in rows}
    check("active worktree entry wins", by_id[7][2] == "lane-live", str(by_id[7]))
    check("top-level branch fallback", by_id[9][2] == "top-branch", str(by_id[9]))
    check("no-branch doc honest-NULL", by_id[11][2] is None, str(by_id[11]))
    check("missing file honest-NULL", by_id[13][2] is None, str(by_id[13]))
    check("map-prefix rewrote paths", str(newdocs) in by_id[7][1], str(by_id[7]))
    check("unmapped path left as-is", by_id[13][1] == "/nonexistent/z.md", str(by_id[13]))

    # Refuse non-empty dest without --force; --force proceeds (and dies on id
    # collision inside the transaction — rollback leaves the table unchanged).
    r = _run_import("--source", str(src), "--dest", str(dest))
    check("refuses non-empty dest", r.returncode == 1 and "non-empty" in r.stderr, r.stderr)
    r = _run_import("--source", str(src), "--dest", str(dest), "--force")
    check("--force collision rolls back", r.returncode == 1 and "rolled back" in r.stderr, r.stderr)
    conn = sqlite3.connect(dest)
    count = conn.execute("SELECT count(*) FROM session_documents").fetchone()[0]
    conn.close()
    check("rollback leaves table unchanged", count == 4, str(count))

    # Missing source / schema-less dest fail loud.
    r = _run_import("--source", str(d / "nope.db"), "--dest", str(dest))
    check(
        "missing source fails loud", r.returncode == 1 and "source DB missing" in r.stderr, r.stderr
    )
    bare = d / "bare.db"
    sqlite3.connect(bare).close()
    r = _run_import("--source", str(src), "--dest", str(bare))
    check(
        "schema-less dest fails loud",
        r.returncode == 1 and "run init_db.py first" in r.stderr,
        r.stderr,
    )


def test_dispatch_bash_syntax() -> None:
    r = subprocess.run(
        ["bash", "-n", str(REPO_ROOT / "cli-tools" / "bin" / "dispatch")],
        capture_output=True,
        text=True,
    )
    check("bash -n dispatch", r.returncode == 0, r.stderr)


def main() -> int:
    for test in (
        test_migration_idempotency,
        test_stamp_helper,
        test_claim_mirror,
        test_import_script,
        test_dispatch_bash_syntax,
    ):
        print(f"— {test.__name__}")
        test()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("\nall green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
