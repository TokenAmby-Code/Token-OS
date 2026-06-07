"""Git + GitHub activity for the daily build.

Pure subprocess wrappers over ``git`` and ``gh`` — no wrapper module exists, so
this mirrors ``cli-tools/bin/custodes-wave-poll``'s gh/git usage. Every function
here is read-only and degrades to an empty result on failure (gh unauthed, base
sha unknown, …) rather than raising — the build note is still worth producing
with whatever activity could be resolved.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta

# Conventional-/squash-merge commits carry a trailing "(#123)"; merge commits say
# "Merge pull request #123". Either way the PR number rides in a "#<n>" token.
_PR_NUM_RE = re.compile(r"#(\d+)")


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def _looks_like_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def repo_head(repo: str, ref: str = "main") -> str:
    """Full sha of ``ref`` (default local ``main``), or '' if unresolvable."""
    proc = _run(["git", "rev-parse", ref], repo)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def commit_date(repo: str, sha: str) -> str:
    """YYYY-MM-DD committer date for ``sha``, or '' if unknown."""
    if not sha:
        return ""
    proc = _run(["git", "show", "-s", "--format=%cs", sha], repo)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def resolve_base(
    repo: str, since: str | None, prior_head_sha: str | None, ref: str = "main"
) -> tuple[str, str]:
    """Resolve the "since last build" base as ``(base_sha, base_date)``.

    Priority: explicit ``--since`` (sha or YYYY-MM-DD) > the prior build's
    recorded ``head_sha`` > bootstrap (last 24h).
    """
    if since:
        if _looks_like_date(since):
            proc = _run(["git", "rev-list", "-1", f"--before={since} 00:00", ref], repo)
            return proc.stdout.strip(), since
        # treat as a sha / ref
        return since, commit_date(repo, since)

    if prior_head_sha:
        return prior_head_sha, commit_date(repo, prior_head_sha)

    # Bootstrap: no prior build, no override → last 24h.
    cutoff = datetime.now() - timedelta(hours=24)
    proc = _run(["git", "rev-list", "-1", "--before=24 hours ago", ref], repo)
    return proc.stdout.strip(), cutoff.strftime("%Y-%m-%d")


def merged_prs(repo: str, base_date: str) -> list[dict]:
    """PRs merged into ``main`` on/after ``base_date`` (newest first)."""
    if not base_date:
        base_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    proc = _run(
        [
            "gh",
            "pr",
            "list",
            "--base",
            "main",
            "--state",
            "merged",
            "--search",
            f"merged:>={base_date}",
            "--json",
            "number,title,headRefName,mergedAt,url",
            "--limit",
            "200",
        ],
        repo,
    )
    if proc.returncode != 0:
        return []
    try:
        prs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []
    prs.sort(key=lambda p: p.get("mergedAt") or "", reverse=True)
    return prs


def open_prs(repo: str) -> list[dict]:
    """Still-open PRs targeting ``main`` (the anti-rollover roll-call)."""
    proc = _run(
        [
            "gh",
            "pr",
            "list",
            "--base",
            "main",
            "--state",
            "open",
            "--json",
            "number,title,headRefName,url,updatedAt",
            "--limit",
            "200",
        ],
        repo,
    )
    if proc.returncode != 0:
        return []
    try:
        prs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []
    prs.sort(key=lambda p: p.get("updatedAt") or "", reverse=True)
    return prs


def commits_in_range(
    repo: str, base_sha: str, ref: str = "main"
) -> list[tuple[str, str, int | None]]:
    """Return ``[(short_sha, subject, pr_number_or_None)]`` for base_sha..ref.

    PR attribution is heuristic: the first ``#<n>`` token in the subject (squash
    merges append ``(#n)``; merge commits say ``Merge pull request #n``).
    """
    rng = f"{base_sha}..{ref}" if base_sha else ref
    proc = _run(["git", "log", rng, "--format=%h%x09%s"], repo)
    if proc.returncode != 0:
        return []
    out: list[tuple[str, str, int | None]] = []
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        match = _PR_NUM_RE.search(subject)
        pr_num = int(match.group(1)) if match else None
        out.append((sha, subject, pr_num))
    return out


def numstat_churn(repo: str, base_sha: str, ref: str = "main") -> list[tuple[str, int]]:
    """Return ``[(repo_relative_path, churn)]`` sorted desc for base_sha..ref."""
    if not base_sha:
        return []
    proc = _run(["git", "diff", "--numstat", f"{base_sha}..{ref}"], repo)
    if proc.returncode != 0:
        return []
    rows: list[tuple[str, int]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        try:
            churn = (0 if added == "-" else int(added)) + (0 if deleted == "-" else int(deleted))
        except ValueError:
            churn = 0
        rows.append((path, churn))
    rows.sort(key=lambda row: row[1], reverse=True)
    return rows
