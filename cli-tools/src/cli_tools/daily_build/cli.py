#!/usr/bin/env python3
"""daily-build CLI — assemble the day's Obsidian-hosted build review note.

Usage:
    daily-build                       # since the last build (or the build date's calendar day)
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

DEFAULT_REPO = str(Path.home() / "runtimes" / "Token-OS" / "live")
DEFAULT_VAULT = str(Path(os.environ.get("IMPERIUM_VAULT", "~/vaults/Imperium-ENV")).expanduser())
BUILDS_SUBDIR = "Terra/Journal/Builds"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _git_toplevel(path: str) -> str:
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _is_code_repo(root: str) -> bool:
    """Sentinel for the Token-OS code repo (the vault is a git repo too —
    silently building against it produces an empty note)."""
    return (Path(root) / "cli-tools" / "pyproject.toml").is_file()


def _resolve_repo(arg: str | None) -> str:
    if arg:
        top = _git_toplevel(arg)
        if top and _is_code_repo(top):
            return top
        print(
            f"error: --repo {arg} is not the Token-OS code repo "
            "(no cli-tools/pyproject.toml at its git toplevel).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    for cand in (os.environ.get("TOKEN_OS_DIR"), os.getcwd(), DEFAULT_REPO):
        if not cand:
            continue
        top = _git_toplevel(cand)
        if top and _is_code_repo(top):
            return top
    print(
        "error: could not auto-detect the Token-OS code repo "
        "(cwd is not inside it — running from the vault?). Pass --repo <path>.",
        file=sys.stderr,
    )
    raise SystemExit(2)


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
        return Path(arg).expanduser()
    env = os.environ.get("TOKEN_API_AGENTS_DB") or os.environ.get("TOKEN_API_DB")
    if env:
        return Path(env).expanduser()
    return (
        Path(os.environ.get("TOKEN_API_DATABASE_DIR") or "~/runtimes/database") / "agents.db"
    ).expanduser()


def _date_arg(value: str) -> str:
    """argparse type for --date: reject malformed dates at the CLI boundary."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None
    return value


MAX_WINDOW_DAYS = 7


def _window_too_wide(base_date: str, build_date: str, max_days: int = MAX_WINDOW_DAYS) -> bool:
    """True when an inherited base anchor would produce an unusably wide window."""
    try:
        base = datetime.strptime(base_date, "%Y-%m-%d")
        build = datetime.strptime(build_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    return (build - base).days > max_days


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


def _github_attribution_zero_warning(
    commits: list[tuple[str, str, int | None]], gh_rows: list[dict], diagnostic: str | None
) -> str | None:
    """Return the non-silent warning for a failed expected GitHub attribution."""
    git_pr_numbers = {pr_number for _, _, pr_number in commits if pr_number is not None}
    if gh_rows or not diagnostic or not git_pr_numbers:
        return None
    return (
        "GitHub attribution=0 despite PR-numbered commits "
        f"({', '.join(f'#{number}' for number in sorted(git_pr_numbers))}): {diagnostic}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="daily-build",
        description="Assemble the day's Obsidian-hosted build review note.",
    )
    parser.add_argument("--since", help="Base override: a sha/ref or YYYY-MM-DD date.")
    parser.add_argument(
        "--date", type=_date_arg, help="Build date, YYYY-MM-DD (default: today, local)."
    )
    parser.add_argument("--repo", help="Token-OS repo root (default: auto-detect).")
    parser.add_argument("--vault", help="Imperium-ENV vault root (default: auto-detect).")
    parser.add_argument(
        "--db",
        help="agents.db path (default: $TOKEN_API_AGENTS_DB, $TOKEN_API_DB, "
        "or ~/runtimes/database/agents.db).",
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

    def warn(msg: str) -> None:
        print(f"[daily-build] warning: {msg}", file=sys.stderr)

    log(f"repo={repo} vault={vault} db={db_path} date={date} ref={args.ref}")

    head_sha = git.repo_head(repo, args.ref)
    if not head_sha:
        print(f"error: could not resolve `{args.ref}` HEAD in {repo}", file=sys.stderr)
        return 1
    remote_main_sha = git.remote_head(repo, args.ref)
    if remote_main_sha and remote_main_sha != head_sha:
        warn(
            f"checkout {args.ref} ({head_sha[:9]}) != remote {args.ref} ({remote_main_sha[:9]}) "
            "— the note covers the checkout, not deployed truth."
        )

    prior_head = _prior_head_sha(builds_dir, date)
    if prior_head and not git.sha_in_repo(repo, prior_head):
        warn(
            f"prior build note's head_sha {prior_head[:9]} is not a commit in {repo} "
            "(note written against another repo?) — ignoring it as a base anchor."
        )
        prior_head = None
    elif prior_head and not git.is_ancestor(repo, prior_head, head_sha):
        warn(
            f"prior build note's head_sha {prior_head[:9]} is not an ancestor of "
            f"{head_sha[:9]} (note written on a divergent branch?) — ignoring it as "
            "a base anchor."
        )
        prior_head = None
    # All ranges walk from the resolved head sha (which may be a detached deploy
    # HEAD ahead of the named ref) so the note's head and its window agree.
    base_sha, base_date = git.resolve_base(repo, args.since, prior_head, head_sha, build_date=date)
    if not args.since and _window_too_wide(base_date, date):
        warn(
            f"inherited base {base_sha[:9] if base_sha else '(none)'} ({base_date}) is more than "
            f"{MAX_WINDOW_DAYS} days before {date} — re-anchoring to the build date's calendar "
            "day. Pass --since to widen deliberately."
        )
        base_sha, base_date = git.resolve_base(repo, None, None, head_sha, build_date=date)
    log(f"base_sha={base_sha or '(none)'} base_date={base_date} head_sha={head_sha or '(none)'}")

    commits = git.commits_in_range(repo, base_sha, head_sha)
    gh_rows, gh_diagnostic = git.merged_prs_result(repo, base_date)
    if warning := _github_attribution_zero_warning(commits, gh_rows, gh_diagnostic):
        warn(warning)
    merged = git.attribute_window(repo, commits, gh_rows, base_sha, head_sha)
    log(f"merged PRs in window: {len(merged)} (git-attributed ∪ gh; gh rows: {len(gh_rows)})")
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

    churn = git.numstat_churn(repo, base_sha, head_sha)
    top_files = _rank_top_files(included, churn, repo)
    open_prs = git.open_prs(repo)
    log(f"commits={len(commits)} top_files={len(top_files)} open_prs={len(open_prs)}")

    note = gen.generate(
        date=date,
        base_sha=base_sha,
        base_date=base_date,
        head_sha=head_sha,
        remote_main_sha=remote_main_sha,
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
