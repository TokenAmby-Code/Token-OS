"""Git + GitHub activity for the daily build.

Pure subprocess wrappers over ``git`` and ``gh`` — no wrapper module exists, so
this mirrors ``cli-tools/bin/custodes-wave-poll``'s gh/git usage. Every function
here is read-only and degrades to an empty result on failure (gh unauthed, base
sha unknown, …) rather than raising — the build note is still worth producing
with whatever activity could be resolved.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta

# Conventional-/squash-merge commits carry a trailing "(#123)"; merge commits say
# "Merge pull request #123". A bare first-"#n" scan misattributes subjects that
# reference issues/rulings mid-sentence ("... (ruling #9) (#717)"), so only
# these two anchored shapes count.
_PR_TAIL_RE = re.compile(r"\(#(\d+)\)\s*$")
_PR_MERGE_RE = re.compile(r"^Merge pull request #(\d+)")


def _pr_num(subject: str) -> int | None:
    match = _PR_TAIL_RE.search(subject) or _PR_MERGE_RE.match(subject)
    return int(match.group(1)) if match else None


def _run(args: list[str], cwd: str, *, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _looks_like_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def repo_head(repo: str, ref: str = "main") -> str:
    """Full sha of ``ref`` (default local ``main``), or '' if unresolvable.

    Deploy checkouts run DETACHED at the deployed sha while their local branch
    ref lags behind (the 2026-07-14 trial reported ``main`` five merges behind
    process truth). When HEAD is detached and descends from ``ref``, HEAD is
    the fresher truth — use it. Branch checkouts (feature worktrees) keep the
    named ref.
    """
    proc = _run(["git", "rev-parse", ref], repo)
    ref_sha = proc.stdout.strip() if proc.returncode == 0 else ""
    if not ref_sha:
        return ""
    if _run(["git", "symbolic-ref", "-q", "HEAD"], repo).returncode == 0:
        return ref_sha  # on a branch — the named ref is the intent
    head_proc = _run(["git", "rev-parse", "HEAD"], repo)
    head_sha = head_proc.stdout.strip() if head_proc.returncode == 0 else ""
    if (
        head_sha
        and head_sha != ref_sha
        and _run(["git", "merge-base", "--is-ancestor", ref_sha, head_sha], repo).returncode == 0
    ):
        return head_sha
    return ref_sha


# ls-remote can hang on network/DNS/SSH/credential prompts; the honesty check
# is best-effort, so a stuck remote must not stall the build.
REMOTE_TIMEOUT_S = 10


def remote_head(repo: str, ref: str = "main") -> str:
    """Full sha of ``ref`` on the first resolvable remote, or '' if offline.

    The checkout can lag deployed truth — this is the note's honesty check
    (checkout head vs remote main), best-effort like everything else here.
    """
    for remote in ("origin", "github"):
        try:
            proc = _run(
                ["git", "ls-remote", remote, f"refs/heads/{ref}"],
                repo,
                timeout=REMOTE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.split()[0]
    return ""


def sha_in_repo(repo: str, sha: str) -> bool:
    """True if ``sha`` names a commit in this repo (guards cross-repo anchors)."""
    if not sha:
        return False
    proc = _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], repo)
    return proc.returncode == 0


def is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    """True if ``ancestor`` is an ancestor of (or equal to) ``descendant``.

    Guards divergent-branch anchors: a prior note written on another branch
    names a commit that exists here but whose ``base..head`` window would not
    mean "since last build".
    """
    if not ancestor or not descendant:
        return False
    proc = _run(["git", "merge-base", "--is-ancestor", ancestor, descendant], repo)
    return proc.returncode == 0


def commit_date(repo: str, sha: str) -> str:
    """YYYY-MM-DD committer date for ``sha``, or '' if unknown."""
    if not sha:
        return ""
    proc = _run(["git", "show", "-s", "--format=%cs", sha], repo)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def commit_iso(repo: str, sha: str) -> str:
    """Strict-ISO committer timestamp for ``sha``, or '' if unknown."""
    if not sha:
        return ""
    proc = _run(["git", "show", "-s", "--format=%cI", sha], repo)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def resolve_base(
    repo: str,
    since: str | None,
    prior_head_sha: str | None,
    ref: str = "main",
    build_date: str | None = None,
) -> tuple[str, str]:
    """Resolve the "since last build" base as ``(base_sha, base_date)``.

    Priority: explicit ``--since`` (sha or YYYY-MM-DD) > the prior build's
    recorded ``head_sha`` > bootstrap (the build date's calendar day: last
    commit before ``build_date`` 00:00, so ``--date`` reruns are reproducible).
    """
    if since:
        if _looks_like_date(since):
            proc = _run(["git", "rev-list", "-1", f"--before={since} 00:00", ref], repo)
            return proc.stdout.strip(), since
        # treat as a sha / ref
        return since, commit_date(repo, since)

    if prior_head_sha:
        return prior_head_sha, commit_date(repo, prior_head_sha)

    # Bootstrap: no prior build, no override → the build date's calendar day.
    anchor = build_date or datetime.now().strftime("%Y-%m-%d")
    proc = _run(["git", "rev-list", "-1", f"--before={anchor} 00:00", ref], repo)
    sha = proc.stdout.strip() if proc.returncode == 0 else ""
    fallback = (datetime.strptime(anchor, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    return sha, commit_date(repo, sha) or fallback


# The runtime checkout's only remote is the local CD bare (a path, not GitHub),
# so bare ``gh`` invoked there resolves no repo and pr-list silently returns
# nothing. Resolve an explicit owner/name slug instead and pass it as --repo on
# every gh call. The hardcoded default is safe: cli.py's _resolve_repo sentinel
# guarantees daily-build only ever runs against the Token-OS code repo.
_GH_URL_RE = re.compile(r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/\s]+?)(?:\.git)?$")
_DEFAULT_GH_SLUG = "TokenAmby-Code/Token-OS"


def _gh_slug(repo: str) -> str:
    """owner/name for ``gh --repo``: GH_REPO env > remote-derived > Token-OS."""
    env = os.environ.get("GH_REPO")
    if env:
        return env
    for remote in ("origin", "github"):
        proc = _run(["git", "remote", "get-url", remote], repo)
        if proc.returncode != 0:
            continue
        match = _GH_URL_RE.match(proc.stdout.strip())
        if match:
            return match.group(1)
    return _DEFAULT_GH_SLUG


def merged_prs_result(repo: str, base_date: str) -> tuple[list[dict], str | None]:
    """Return merged PRs plus a diagnostic when GitHub supplied no usable data.

    Callers must not mistake an unavailable or empty GitHub query for successful
    attribution.  The list-only ``merged_prs`` wrapper remains for consumers
    that do not need to surface that distinction.
    """
    if not base_date:
        base_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    slug = _gh_slug(repo)
    proc = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--base",
            "main",
            "--state",
            "merged",
            "--search",
            f"merged:>={base_date}",
            "--json",
            "number,title,headRefName,mergedAt,url,mergeCommit",
            "--limit",
            "200",
        ],
        repo,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "no stderr"
        return [], f"gh pr list --repo {slug} exited {proc.returncode}: {detail}"
    try:
        prs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return [], "gh pr list returned invalid JSON"
    if not prs:
        return [], f"gh pr list --repo {slug} returned 0 merged PR rows"
    prs.sort(key=lambda p: p.get("mergedAt") or "", reverse=True)
    return prs, None


def merged_prs(repo: str, base_date: str) -> list[dict]:
    """PRs merged into ``main`` on/after ``base_date`` (newest first)."""
    return merged_prs_result(repo, base_date)[0]


def open_prs(repo: str) -> list[dict]:
    """Still-open PRs targeting ``main`` (the anti-rollover roll-call)."""
    proc = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            _gh_slug(repo),
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

    PR attribution comes from the subject's anchored merge shapes only (trailing
    ``(#n)`` or ``Merge pull request #n``) — see ``_pr_num``.
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
        out.append((sha, subject, _pr_num(subject)))
    return out


def window_shas(repo: str, base_sha: str, ref: str = "main") -> set[str]:
    """Full shas of every commit in ``base_sha..ref`` (exact window membership)."""
    rng = f"{base_sha}..{ref}" if base_sha else ref
    proc = _run(["git", "log", rng, "--format=%H"], repo)
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def web_url(repo: str) -> str:
    """https URL of the repo's GitHub home (same slug resolution as gh calls)."""
    return f"https://github.com/{_gh_slug(repo)}"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strip_pr_suffix(subject: str) -> str:
    """Drop the squash-merge ``(#123)`` tail so a synthesized title reads clean."""
    return re.sub(r"\s*\(#\d+\)\s*$", "", subject).strip()


def attribute_window(
    repo: str,
    commits: list[tuple[str, str, int | None]],
    gh_prs: list[dict],
    base_sha: str,
    head_sha: str,
) -> list[dict]:
    """Merged-PR list for the window: git commit truth gates, gh metadata enriches.

    PR numbers found in ``base..head`` commit subjects are the authoritative
    attribution set — a PR-numbered commit is NEVER dropped because gh (or the
    thread/session-doc join) resolved nothing for it. gh rows enrich matching
    numbers with title/branch/url; numbers gh missed are synthesized from the
    commit itself. gh-only rows (no anchored ``#n`` token in any subject) are
    admitted only when their merge commit actually sits in ``base..head`` (exact
    git truth; timestamp containment is the fallback when gh reports no merge
    commit), which also trims the date-search overshoot of ``merged:>=<date>`` —
    including the base commit's own PR, whose ``mergedAt`` can trail its
    committer timestamp by a beat.
    """
    window_nums: dict[int, tuple[str, str]] = {}
    for sha, subject, num in commits:
        if num is not None and num not in window_nums:
            window_nums[num] = (sha, subject)

    by_num = {pr.get("number"): pr for pr in gh_prs}
    url_base = web_url(repo)
    out: list[dict] = []
    for num, (sha, subject) in window_nums.items():
        gh_pr = by_num.get(num)
        if gh_pr is None:
            out.append(
                {
                    "number": num,
                    "title": _strip_pr_suffix(subject),
                    "headRefName": "",
                    "url": f"{url_base}/pull/{num}" if url_base else "",
                    "mergedAt": commit_iso(repo, sha),
                    "attribution": "git",
                }
            )
        else:
            out.append({**gh_pr, "attribution": "git+gh"})

    in_window = window_shas(repo, base_sha, head_sha)
    base_ts = _parse_iso(commit_iso(repo, base_sha))
    head_ts = _parse_iso(commit_iso(repo, head_sha))
    for pr in gh_prs:
        if pr.get("number") in window_nums:
            continue
        merge_oid = (pr.get("mergeCommit") or {}).get("oid") or ""
        if merge_oid:
            if merge_oid not in in_window:
                continue
        else:
            merged_ts = _parse_iso(pr.get("mergedAt"))
            if merged_ts is None:
                continue
            if base_ts is not None and merged_ts <= base_ts:
                continue
            if head_ts is not None and merged_ts > head_ts:
                continue
        out.append({**pr, "attribution": "gh"})

    out.sort(key=lambda pr: pr.get("mergedAt") or "", reverse=True)
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
