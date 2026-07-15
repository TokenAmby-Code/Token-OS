#!/usr/bin/env python3
"""One-shot session_documents import for the k12-era registry cutover (rung 1).

Run ONCE by hand at cutover (doc §6 step 7) — NEVER referenced from init/main:

    uv run --directory token-api python import_session_documents.py \
        --source <mac-snapshot.db> --dest <k12 agents.db> \
        [--map-prefix /Users/tokenclaw=/home/<user>] [--dry-run] [--force]

Contract:
- Source is opened read-only (file:...?mode=ro); missing file or missing
  session_documents table fails loud.
- Dest schema must already exist WITH the branch column (operator runs
  init_db.py on rung-1 code first); this script never creates schema.
- Refuses a non-empty dest session_documents unless --force.
- Copies the PRAGMA-intersected column set PRESERVING id — frontmatter
  session_doc_id keys must keep matching their rows.
- --map-prefix OLD=NEW does an exact prefix rewrite of file_path; rows whose
  path doesn't start with OLD are counted as unmapped and left as-is.
- Branch backfill is EXACT-only from each doc's own frontmatter: active
  worktrees entry's branch > last entry with a branch > top-level branch: >
  honest NULL (missing/unparseable file -> NULL + warning). No slugify, no
  LIKE, no guessing (k12-era R4 honest-NULL).
- Validates inserted == source row count or rolls back and dies. --dry-run
  computes and prints the same summary without writing anything.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from session_doc_helpers import parse_frontmatter


def fail(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def parse_map_prefix(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        fail("--map-prefix must be OLD=NEW (exact prefix rewrite)")
    old, new = spec.split("=", 1)
    if not old:
        fail("--map-prefix OLD side must be non-empty")
    return old, new


def branch_from_frontmatter(fp: Path) -> str | None:
    """EXACT-only branch backfill from a doc's own frontmatter.

    Priority: active worktrees entry's branch > last entry with a branch >
    top-level branch: > None. Any read/parse failure is the caller's warning.
    """
    content = fp.read_text(encoding="utf-8")
    fm, _body = parse_frontmatter(content)
    wts = fm.get("worktrees")
    if isinstance(wts, list):
        entries = [w for w in wts if isinstance(w, dict)]
        for w in entries:
            if w.get("status") == "active" and w.get("branch"):
                return str(w["branch"])
        for w in reversed(entries):
            if w.get("branch"):
                return str(w["branch"])
    if fm.get("branch"):
        return str(fm["branch"])
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="Mac snapshot agents.db (opened read-only)")
    ap.add_argument("--dest", required=True, help="k12 agents.db (schema must already exist)")
    ap.add_argument("--map-prefix", help="OLD=NEW exact prefix rewrite for file_path")
    ap.add_argument("--dry-run", action="store_true", help="compute + print summary, write nothing")
    ap.add_argument("--force", action="store_true", help="allow a non-empty dest table")
    args = ap.parse_args()

    src_path = Path(args.source).expanduser()
    if not src_path.is_file():
        fail(f"source DB missing: {src_path}")
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    if not src.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_documents'"
    ).fetchone():
        fail(f"source DB has no session_documents table: {src_path}")

    dest_path = Path(args.dest).expanduser()
    if not dest_path.is_file():
        fail(
            f"dest DB missing: {dest_path} — run init_db.py first; this script never creates schema"
        )
    dest = sqlite3.connect(dest_path)
    dest_cols = [r[1] for r in dest.execute("PRAGMA table_info(session_documents)")]
    if not dest_cols:
        fail("dest DB has no session_documents table — run init_db.py first")
    if "branch" not in dest_cols:
        fail("dest session_documents lacks the branch column — run init_db.py on rung-1 code first")

    existing = dest.execute("SELECT count(*) FROM session_documents").fetchone()[0]
    if existing and not args.force:
        fail(
            f"dest session_documents is non-empty ({existing} rows); pass --force to import anyway"
        )

    src_cols = [r[1] for r in src.execute("PRAGMA table_info(session_documents)")]
    copy_cols = [c for c in src_cols if c in dest_cols]
    if "id" not in copy_cols or "file_path" not in copy_cols:
        fail(f"intersected columns lack id/file_path: {copy_cols}")

    rows = src.execute(
        f"SELECT {', '.join(copy_cols)} FROM session_documents ORDER BY id"
    ).fetchall()
    source_count = len(rows)

    map_old = map_new = None
    if args.map_prefix:
        map_old, map_new = parse_map_prefix(args.map_prefix)

    stats = {
        "imported": 0,
        "branch_stamped": 0,
        "branch_null": 0,
        "files_missing": 0,
        "paths_unmapped": 0,
    }
    fp_idx = copy_cols.index("file_path")
    br_idx = copy_cols.index("branch") if "branch" in copy_cols else None
    insert_cols = copy_cols if br_idx is not None else [*copy_cols, "branch"]

    out_rows = []
    for row in rows:
        row = list(row)
        fp = row[fp_idx]
        if map_old is not None and fp:
            if fp.startswith(map_old):
                fp = map_new + fp[len(map_old) :]
                row[fp_idx] = fp
            else:
                stats["paths_unmapped"] += 1
        branch = row[br_idx] if br_idx is not None else None
        if not branch:
            branch = None
            path = Path(fp) if fp else None
            if path is None or not path.is_file():
                stats["files_missing"] += 1
                print(f"WARN: doc file missing, branch stays NULL: {fp}", file=sys.stderr)
            else:
                try:
                    branch = branch_from_frontmatter(path)
                except Exception as exc:
                    print(
                        f"WARN: unreadable/unparseable frontmatter, branch stays NULL: {fp}: {exc}",
                        file=sys.stderr,
                    )
        if branch:
            stats["branch_stamped"] += 1
        else:
            stats["branch_null"] += 1
        if br_idx is not None:
            row[br_idx] = branch
            out_rows.append(row)
        else:
            out_rows.append([*row, branch])

    stats["imported"] = source_count
    if args.dry_run:
        print(json.dumps({"dry_run": True, **stats}, indent=2))
        return

    placeholders = ", ".join("?" for _ in insert_cols)
    try:
        with dest:  # one transaction: commit on success, rollback on any raise
            dest.executemany(
                f"INSERT INTO session_documents ({', '.join(insert_cols)}) VALUES ({placeholders})",
                out_rows,
            )
            inserted = (
                dest.execute("SELECT count(*) FROM session_documents").fetchone()[0] - existing
            )
            if inserted != source_count:
                raise RuntimeError(f"inserted {inserted} rows != source {source_count}")
    except Exception as exc:
        fail(f"import failed, rolled back: {exc}")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
