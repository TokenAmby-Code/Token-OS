#!/usr/bin/env python3
"""daily-build CLI — assemble the day's Obsidian-hosted build review note.

Usage:
    daily-build                       # since the last build (or last 24h)
    daily-build --since <sha|date>    # override the base
    daily-build --date 2026-06-07     # assemble for a specific day (default: today)
    daily-build --dry-run             # print the note to stdout, write nothing

Writes ``Terra/Journal/Builds/<date>.md`` (the satellite of that day's daily
note). Re-running the same day regenerates the note in place (idempotent).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import git_activity as git
from . import review_generator as gen
from .session_doc_resolver import (
    CORE_CHANGE_HEADINGS,
    KEY_FILES_HEADINGS,
    build_diagram_index,
    diagram_docs_for,
    diagram_sections,
    extract_headings,
    extract_key_files,
    first_present,
    read_frontmatter,
    resolve_doc_for_branch,
)

DEFAULT_REPO = "/Volumes/Imperium/Token-OS"
DEFAULT_VAULT = "/Volumes/Imperium/Imperium-ENV"
BUILDS_SUBDIR = "Terra/Journal/Builds"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _resolve_repo(arg: str | None) -> str:
    for cand in (arg, os.environ.get("TOKEN_OS_DIR"), os.getcwd(), DEFAULT_REPO):
        if not cand:
            continue
        proc = subprocess.run(
            ["git", "-C", cand, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    return arg or DEFAULT_REPO


def _resolve_vault(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    for env in ("IMPERIUM_ENV", "IMPERIUM_VAULT"):
        val = os.environ.get(env)
        if val:
            return Path(val)
    imperium = os.environ.get("IMPERIUM")
    if imperium and (Path(imperium) / "Imperium-ENV").is_dir():
        return Path(imperium) / "Imperium-ENV"
    return Path(DEFAULT_VAULT)


def _resolve_db(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env = os.environ.get("TOKEN_API_DB")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "agents.db"


def _is_skip(fm: dict) -> bool:
    val = fm.get("daily_build_skip")
    return val is True or str(val).strip().lower() == "true"


def _prior_head_sha(builds_dir: Path, today: str) -> str | None:
    """Most recent prior build note's recorded head_sha (the new base)."""
    if not builds_dir.is_dir():
        return None
    candidates = [
        path for path in builds_dir.glob("*.md") if _DATE_RE.match(path.stem) and path.stem < today
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stem, reverse=True)
    fm, _ = read_frontmatter(candidates[0])
    head = fm.get("head_sha")
    return str(head) if head else None


def _rel(path: str, repo: str) -> str:
    prefix = repo.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def _build_thread(pr: dict, doc_path: str | None, vault: Path, reverse_index: dict) -> dict:
    thread: dict = {
        "pr": pr,
        "branch": pr.get("headRefName", ""),
        "doc_path": doc_path,
        "stem": None,
        "title": None,
        "skip": False,
        "key_files": [],
        "key_files_heading": None,
        "core_change_headings": [],
        "diagrams": [],
    }
    if not doc_path:
        return thread

    path = Path(doc_path)
    fm, body = read_frontmatter(path)
    thread["stem"] = path.stem
    thread["title"] = fm.get("title") or path.stem
    thread["skip"] = _is_skip(fm)
    if thread["skip"]:
        return thread

    headings = extract_headings(body)
    thread["key_files_heading"] = first_present(headings, KEY_FILES_HEADINGS)
    thread["key_files"] = extract_key_files(body)

    core: list[str] = []
    for cand in CORE_CHANGE_HEADINGS:
        hit = first_present(headings, (cand,))
        if hit and hit not in core:
            core.append(hit)
    thread["core_change_headings"] = core

    diagrams = []
    for diag in diagram_docs_for(path, fm, body, vault, reverse_index):
        sections = diagram_sections(diag)
        if sections:
            diagrams.append((diag.stem, sections))
    thread["diagrams"] = diagrams
    return thread


def _rank_top_files(
    threads: list[dict], churn: list[tuple[str, int]], repo: str
) -> list[tuple[str, str]]:
    ranked: list[tuple[str, str]] = []
    seen: set[str] = set()
    for thread in threads:
        stem = thread.get("stem")
        for key_file in thread.get("key_files", []):
            rel = _rel(key_file, repo)
            if rel in seen:
                continue
            seen.add(rel)
            src = f"from [[{stem}]] Key Files" if stem else "from a Key Files section"
            ranked.append((rel, src))
    for path, amount in churn:
        if path in seen:
            continue
        seen.add(path)
        ranked.append((path, f"diff churn +{amount}"))
    return ranked


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="daily-build",
        description="Assemble the day's Obsidian-hosted build review note.",
    )
    parser.add_argument("--since", help="Base override: a sha/ref or YYYY-MM-DD date.")
    parser.add_argument("--date", help="Build date (default: today, local).")
    parser.add_argument("--repo", help="Token-OS repo root (default: auto-detect).")
    parser.add_argument("--vault", help="Imperium-ENV vault root (default: auto-detect).")
    parser.add_argument(
        "--db", help="agents.db path (default: $TOKEN_API_DB or ~/.claude/agents.db)."
    )
    parser.add_argument("--ref", default="main", help="Branch tip to build to (default: main).")
    parser.add_argument("--dry-run", action="store_true", help="Print the note; write nothing.")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Log resolution steps to stderr."
    )
    args = parser.parse_args()

    repo = _resolve_repo(args.repo)
    vault = _resolve_vault(args.vault)
    db_path = _resolve_db(args.db)
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    builds_dir = vault / BUILDS_SUBDIR

    def log(msg: str) -> None:
        if args.verbose:
            print(f"[daily-build] {msg}", file=sys.stderr)

    log(f"repo={repo} vault={vault} db={db_path} date={date} ref={args.ref}")

    prior_head = _prior_head_sha(builds_dir, date)
    base_sha, base_date = git.resolve_base(repo, args.since, prior_head, args.ref)
    head_sha = git.repo_head(repo, args.ref)
    log(f"base_sha={base_sha or '(none)'} base_date={base_date} head_sha={head_sha or '(none)'}")
    if not head_sha:
        print(f"error: could not resolve `{args.ref}` HEAD in {repo}", file=sys.stderr)
        return 1

    merged = git.merged_prs(repo, base_date)
    log(f"merged PRs in window: {len(merged)}")
    reverse_index = build_diagram_index(vault)

    included: list[dict] = []
    opted_out: list[dict] = []
    for pr in merged:
        doc_path = resolve_doc_for_branch(db_path, pr.get("headRefName", ""))
        thread = _build_thread(pr, doc_path, vault, reverse_index)
        if thread["skip"]:
            opted_out.append(thread)
            log(f"  #{pr['number']} {pr['headRefName']} → OPTED OUT ({thread['stem']})")
        else:
            included.append(thread)
            log(f"  #{pr['number']} {pr['headRefName']} → {thread['stem'] or 'no session doc'}")

    commits = git.commits_in_range(repo, base_sha, args.ref)
    churn = git.numstat_churn(repo, base_sha, args.ref)
    top_files = _rank_top_files(included, churn, repo)
    open_prs = git.open_prs(repo)
    log(f"commits={len(commits)} top_files={len(top_files)} open_prs={len(open_prs)}")

    note = gen.generate(
        date=date,
        base_sha=base_sha,
        base_date=base_date,
        head_sha=head_sha,
        ref=args.ref,
        threads=included,
        opted_out=opted_out,
        commits=commits,
        top_files=top_files,
        open_prs=open_prs,
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )

    if args.dry_run:
        sys.stdout.write(note)
        return 0

    build_path = builds_dir / f"{date}.md"
    existed = build_path.exists()
    builds_dir.mkdir(parents=True, exist_ok=True)
    # Direct write (mirrors `obsidian create`'s printf>file): the obsidian CLI's
    # `create` refuses to overwrite, which would break same-day idempotent
    # regeneration, and rich content (![[…]], code fences) survives a file write
    # cleanly. The note routes to the same vault path the CLI would resolve.
    build_path.write_text(note, encoding="utf-8")
    rel = build_path.relative_to(vault)
    print(
        f"{'Updated' if existed else 'Created'}: {rel} "
        f"({len(included)} thread(s), {len(opted_out)} opted out, "
        f"{len(open_prs)} open PR(s))"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
