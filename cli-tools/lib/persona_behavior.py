from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PersonaRow:
    slug: str
    display_name: str
    default_rank: str


_REQUIRED_COLUMNS = {"slug", "display_name", "default_rank"}


def _db_path() -> Path:
    return Path(
        os.environ.get("TOKEN_API_AGENTS_DB")
        or os.environ.get("TOKEN_API_DB")
        or Path(os.environ.get("TOKEN_API_DATABASE_DIR") or "~/runtimes/database") / "agents.db"
    ).expanduser()


def _connect(db_path: Path | None = None) -> sqlite3.Connection | None:
    path = db_path or _db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if table not in {"personas", "primarchs"}:
        return set()
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def invariant_applicable(conn: sqlite3.Connection) -> bool:
    return _REQUIRED_COLUMNS.issubset(_table_columns(conn, "personas"))


def title_slug(slug: str) -> str:
    return "-".join(part[:1].upper() + part[1:] for part in slug.split("-") if part)


def _safe_path_component(value: str) -> str:
    value = value.strip()
    if not value or value in {".", ".."}:
        return ""
    if "/" in value or "\\" in value or Path(value).is_absolute():
        return ""
    if any(part in {".", ".."} for part in Path(value).parts):
        return ""
    return value


def _candidate(personas_root: Path, component: str) -> Path | None:
    safe = _safe_path_component(component)
    if not safe:
        return None
    return personas_root / f"{safe}.md"


def _existing_candidates(root: Path, components: Iterable[str]) -> list[Path]:
    candidates: list[Path] = []
    for component in components:
        candidate = _candidate(root, component)
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _imperium_root() -> Path:
    """Canonical Token-Fleet directory for Imperium persona definitions."""
    checkout = os.environ.get("TOKEN_FLEET_CHECKOUT") or "~/runtimes/Token-Fleet/live"
    return Path(checkout).expanduser() / "shared" / "personas"


def _pax_root() -> Path:
    base = os.environ.get("CIVIC")
    if base:
        return Path(base) / "Pax-ENV"
    return Path("/Volumes/Civic/Pax-ENV")


def _first_existing(candidates: Iterable[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def behavior_file_for(row: PersonaRow) -> tuple[Path | None, list[Path]]:
    slug = _safe_path_component(row.slug.strip().lower())
    rank = (row.default_rank or "astartes").strip().lower()
    display = _safe_path_component(row.display_name.strip())
    titled = title_slug(slug) if slug else ""

    if slug in {"pax", "orchestrator"}:
        root = _pax_root() / "Personas"
        candidates = _existing_candidates(root, [slug, titled, display])
        return _first_existing(candidates), candidates

    root = _imperium_root()
    if rank == "astartes":
        candidates = _existing_candidates(root, [titled, display, slug])
        candidates.append(root / "Astartes.md")
        return _first_existing(candidates), candidates

    if rank in {"overseer", "primarch"}:
        candidates = _existing_candidates(root, [titled, display, slug])
        return _first_existing(candidates), candidates

    candidates = _existing_candidates(root, [titled])
    return _first_existing(candidates), candidates


def _root_for(row: PersonaRow) -> Path:
    """The persona-definition root for a persona — Pax for Civic orchestrators,
    Token-Fleet for everyone else. Mirrors behavior_file_for so a persona's
    rank doc resolves under the same root as its behavior doc."""
    slug = _safe_path_component(row.slug.strip().lower())
    if slug in {"pax", "orchestrator"}:
        return _pax_root() / "Personas"
    return _imperium_root()


def rank_file_for(row: PersonaRow) -> Path | None:
    """Resolve <personas-root>/Ranks/<Rank>.md for a persona's default_rank.

    Ranks are Overseer/Astartes/Primarch. Resolution is case-insensitive so a
    default_rank stored as 'OVERSEER' / 'overseer' / 'Overseer' all land on
    Overseer.md. Returns None when no rank doc exists (the invariant fails closed
    on that at preflight; the wrapper fails loud-but-open at runtime)."""
    rank = _safe_path_component((row.default_rank or "astartes").strip())
    if not rank:
        return None
    ranks_dir = _root_for(row) / "Ranks"
    if not ranks_dir.is_dir():
        return None
    # Scan for the real on-disk entry rather than constructing a path: on a
    # case-insensitive FS (macOS) `(dir / "OVERSEER.md").is_file()` is true but
    # leaks the constructed casing, so the canonical Overseer.md name is the dir
    # entry. Prefer the title-cased match, then any case-insensitive hit.
    target = f"{rank.lower()}.md"
    titled = f"{rank[:1].upper() + rank[1:]}.md"
    matches = [
        entry
        for entry in sorted(ranks_dir.iterdir())
        if entry.is_file() and entry.name.lower() == target
    ]
    for entry in matches:
        if entry.name == titled:
            return entry
    return matches[0] if matches else None


def extract_body(path: Path) -> str:
    """Extract the doctrine body from a persona OR rank doc, identically.

    Ported from the bash extract_persona_system_prompt: prefer a `## System
    Prompt` section (up to the next H2); else strip a leading `--- ... ---`
    frontmatter block; else return the whole body. Rank docs carry no marker and
    no frontmatter, so they round-trip as their whole body."""
    content = path.read_text(encoding="utf-8", errors="replace")
    marker = "## System Prompt"
    idx = content.find(marker)
    if idx < 0:
        parts = content.split("---", 2)
        return parts[2].strip() if len(parts) >= 3 else content.strip()
    rest = content[idx + len(marker) :].strip()
    next_h2 = rest.find("\n## ")
    return rest[:next_h2].strip() if next_h2 >= 0 else rest


def system_doc_for(name: str, db_path: Path | None = None) -> str | None:
    """Assemble the rank+persona system-doc staple for a persona, rank FIRST.

    This is the one code path every launch surface (workers, codex, singletons)
    shares. Returns None when the persona is unknown/unmanaged, or when either
    half (behavior doc / rank doc) can't be resolved — the wrapper treats an
    empty doc for a resolved persona as a loud-but-open runtime warning, while
    preflight + doctor fail closed via invariant_issues()."""
    row = resolve_persona(name, db_path)
    if row is None:
        return None
    behavior, _ = behavior_file_for(row)
    rank = rank_file_for(row)
    if behavior is None or rank is None:
        return None
    return f"{extract_body(rank)}\n\n---\n\n{extract_body(behavior)}"


def iter_persona_rows(conn: sqlite3.Connection) -> list[PersonaRow]:
    if not invariant_applicable(conn):
        return []
    rows = conn.execute(
        """
        SELECT slug, COALESCE(display_name, slug) AS display_name,
               COALESCE(default_rank, 'astartes') AS default_rank
        FROM personas
        WHERE COALESCE(slug, '') != ''
        ORDER BY slug
        """
    ).fetchall()
    return [
        PersonaRow(str(r["slug"]), str(r["display_name"]), str(r["default_rank"])) for r in rows
    ]


def invariant_issues(db_path: Path | None = None) -> list[str]:
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        if not invariant_applicable(conn):
            return []
        issues: list[str] = []
        for row in iter_persona_rows(conn):
            resolved, candidates = behavior_file_for(row)
            if resolved is None:
                candidate_text = ", ".join(str(p) for p in candidates)
                issues.append(
                    f"persona behavior file missing: slug={row.slug} default_rank={row.default_rank} searched=[{candidate_text}]"
                )
            # Every managed persona must also resolve a rank doc — the staple is
            # rank+persona, so a missing rank doc is as fatal as a missing
            # behavior doc. Fail closed here (preflight + tmuxctl doctor consume
            # this); the wrapper fails loud-but-open at runtime.
            if rank_file_for(row) is None:
                ranks_dir = _root_for(row) / "Ranks"
                rank_name = (row.default_rank or "astartes").strip()
                titled = rank_name[:1].upper() + rank_name[1:] if rank_name else rank_name
                issues.append(
                    f"persona rank doc missing: slug={row.slug} default_rank={row.default_rank} "
                    f"searched=[{ranks_dir / f'{titled}.md'}]"
                )
        return issues
    finally:
        conn.close()


def resolve_persona(input_name: str, db_path: Path | None = None) -> PersonaRow | None:
    key = input_name.strip().lower().replace("_", "-").replace(" ", "-")
    raw = input_name.strip().lower()
    conn = _connect(db_path)
    if conn is None:
        return None
    try:
        if invariant_applicable(conn):
            row = conn.execute(
                """
                SELECT slug, COALESCE(display_name, slug) AS display_name,
                       COALESCE(default_rank, 'astartes') AS default_rank
                FROM personas
                WHERE LOWER(slug) = ? OR LOWER(display_name) = ?
                ORDER BY CASE WHEN LOWER(slug) = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (key, raw, key),
            ).fetchone()
            if row:
                return PersonaRow(
                    str(row["slug"]), str(row["display_name"]), str(row["default_rank"])
                )
        # Compatibility with legacy primarchs lookup while callers migrate.
        if {"name"}.issubset(_table_columns(conn, "primarchs")):
            raw_like = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            row = conn.execute(
                """
                SELECT name
                FROM primarchs
                WHERE LOWER(name) = ? OR LOWER(COALESCE(aliases, '')) LIKE ? ESCAPE '\\'
                ORDER BY CASE WHEN LOWER(name) = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (raw, f'%"{raw_like}"%', raw),
            ).fetchone()
            if row:
                name = str(row["name"])
                return PersonaRow(name.lower().replace(" ", "-"), name, "primarch")
        return None
    finally:
        conn.close()


def resolve_behavior_file(input_name: str, db_path: Path | None = None) -> Path | None:
    row = resolve_persona(input_name, db_path)
    if row is None:
        return None
    resolved, _ = behavior_file_for(row)
    return resolved


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    res = sub.add_parser("resolve")
    res.add_argument("name")
    doc = sub.add_parser("system-doc")
    doc.add_argument("name")
    rank = sub.add_parser("rank-file")
    rank.add_argument("name")
    args = parser.parse_args()
    if args.cmd == "check":
        issues = invariant_issues()
        for issue in issues:
            print(issue)
        sys.exit(1 if issues else 0)
    if args.cmd == "resolve":
        row = resolve_persona(args.name)
        if row is None:
            sys.exit(1)
        path = resolve_behavior_file(args.name)
        if path is None:
            sys.exit(2)
        print(f"{row.display_name}\t{path}")
    if args.cmd == "system-doc":
        # The wrapper shells out to this to assemble the rank+persona staple.
        # Exit 3 with no output when the doc can't be built (unknown persona or
        # a missing half) so the wrapper sees empty stdout and fails loud-open.
        document = system_doc_for(args.name)
        if not document:
            sys.exit(3)
        print(document)
    if args.cmd == "rank-file":
        row = resolve_persona(args.name)
        if row is None:
            sys.exit(1)
        path = rank_file_for(row)
        if path is None:
            sys.exit(2)
        print(path)
