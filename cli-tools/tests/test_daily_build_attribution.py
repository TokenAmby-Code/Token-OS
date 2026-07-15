#!/usr/bin/env python3
"""daily-build fix-lane regressions (2026-07-14 trial findings).

Covers: git-truth attribution union (finding 4), gh-window overshoot trim,
bundle never silently drops PR-numbered commits, repo sentinel (finding 2),
window-width cap (finding 3), and checkout-vs-remote head labeling (finding 5).

Run directly: uv run --directory cli-tools python tests/test_daily_build_attribution.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cli_tools.daily_build import git_activity as git  # noqa: E402
from cli_tools.daily_build import review_generator as gen  # noqa: E402
from cli_tools.daily_build.cli import _is_code_repo, _window_too_wide  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILURES.append(name)


def _patch_git(
    iso_by_sha: dict[str, str],
    url: str = "https://github.com/org/repo",
    in_window: set[str] | None = None,
):
    """Stub the subprocess-backed helpers attribute_window leans on."""
    git.commit_iso = lambda repo, sha: iso_by_sha.get(sha, "")  # type: ignore[assignment]
    git.web_url = lambda repo: url  # type: ignore[assignment]
    git.window_shas = lambda repo, base, ref="main": in_window or set()  # type: ignore[assignment]


def test_git_truth_survives_gh_outage() -> None:
    """The trial's exact failure: 0 gh rows must no longer yield 0 attribution."""
    commits = [
        ("aaaa111", "feat(x): thing one (#101)", 101),
        ("bbbb222", "fix(y): thing two (#100)", 100),
        ("cccc333", "chore: no pr token", None),
    ]
    _patch_git({"aaaa111": "2026-07-14T10:00:00-07:00", "bbbb222": "2026-07-14T09:00:00-07:00"})
    out = git.attribute_window("/repo", commits, [], "base", "head")
    check("gh outage: both PR-numbered commits attributed", len(out) == 2, f"got {len(out)}")
    nums = {pr["number"] for pr in out}
    check("gh outage: numbers 100+101 present", nums == {100, 101}, str(nums))
    synth = next(pr for pr in out if pr["number"] == 101)
    check("synth title stripped of (#n)", synth["title"] == "feat(x): thing one", synth["title"])
    check("synth url derived from remote", synth["url"].endswith("/pull/101"), synth["url"])
    check("synth attribution tagged git", synth["attribution"] == "git")
    check("synth branch empty (doc join enriches, never gates)", synth["headRefName"] == "")


def test_gh_enriches_matching_numbers() -> None:
    commits = [("aaaa111", "feat: thing (#5)", 5)]
    gh_rows = [
        {
            "number": 5,
            "title": "gh title",
            "headRefName": "my-branch",
            "url": "u5",
            "mergedAt": "2026-07-14T17:00:00Z",
        }
    ]
    _patch_git({"aaaa111": "2026-07-14T10:00:00-07:00"})
    out = git.attribute_window("/repo", commits, gh_rows, "base", "head")
    check("gh enrichment: single row", len(out) == 1, f"got {len(out)}")
    check("gh enrichment: branch carried", out[0]["headRefName"] == "my-branch")
    check("gh enrichment: tagged git+gh", out[0]["attribution"] == "git+gh")


def test_gh_overshoot_trimmed_by_timestamps() -> None:
    """merged:>=<date> is coarse; no-mergeCommit rows fall back to timestamps."""
    gh_rows = [
        {"number": 1, "title": "before base", "mergedAt": "2026-07-13T08:00:00Z"},
        {"number": 2, "title": "inside window", "mergedAt": "2026-07-14T12:00:00Z"},
        {"number": 3, "title": "after head", "mergedAt": "2026-07-15T09:00:00Z"},
    ]
    _patch_git(
        {"base": "2026-07-13T23:00:00+00:00", "head": "2026-07-14T23:00:00+00:00"},
    )
    out = git.attribute_window("/repo", [], gh_rows, "base", "head")
    nums = {pr["number"] for pr in out}
    check("overshoot: only in-window gh-only row kept", nums == {2}, str(nums))
    check("gh-only attribution tagged gh", out[0]["attribution"] == "gh")


def test_gh_only_gated_by_merge_commit_membership() -> None:
    """The base commit's own PR (mergedAt a beat after committer time) must not leak in."""
    gh_rows = [
        # base's own PR: mergedAt AFTER base committer ts, but oid not in window
        {
            "number": 710,
            "title": "base pr",
            "mergedAt": "2026-07-13T23:00:05Z",
            "mergeCommit": {"oid": "basefullsha"},
        },
        # genuinely in-window gh-only row
        {
            "number": 711,
            "title": "in window",
            "mergedAt": "2026-07-14T10:00:00Z",
            "mergeCommit": {"oid": "goodfullsha"},
        },
    ]
    _patch_git(
        {"base": "2026-07-13T23:00:00+00:00", "head": "2026-07-14T23:00:00+00:00"},
        in_window={"goodfullsha"},
    )
    out = git.attribute_window("/repo", [], gh_rows, "base", "head")
    nums = {pr["number"] for pr in out}
    check("mergeCommit gate: base's own PR excluded", nums == {711}, str(nums))


def test_pr_num_anchored_shapes_only() -> None:
    """First-#n scan misattributed '(ruling #9) (#717)' to PR 9 in live validation."""
    cases = [
        ("feat(cd): converge OFF nodes (ruling #9) (#717)", 717),
        ("Merge pull request #701 from org/branch", 701),
        ("fix: plain squash tail (#728)", 728),
        ("chore: references issue #42 mid-sentence", None),
        ("chore: no token at all", None),
    ]
    for subject, expected in cases:
        got = git._pr_num(subject)
        check(f"pr_num: {subject[:40]!r} → {expected}", got == expected, f"got {got}")


def test_bundle_never_drops_pr_numbered_commits() -> None:
    """Old bug: by_pr groups without a thread vanished from the bundle entirely."""
    commits = [
        ("aaaa111", "feat: orphaned merge (#77)", 77),
        ("bbbb222", "chore: tokenless", None),
    ]
    section = gen._bundle_section([], commits, "base", "2026-07-13", "headsha99", "", "main")
    check("bundle: orphaned #77 commit surfaces", "#77" in section and "aaaa111" in section)
    check("bundle: tokenless commit in unattributed", "bbbb222" in section)


def test_bundle_labels_checkout_and_divergence() -> None:
    section = gen._bundle_section([], [], "base", "2026-07-13", "headsha99", "remotesha", "main")
    check("bundle: head labeled checkout", "checkout `headsha99`" in section)
    check("bundle: divergence warning fires", "not deployed truth" in section)
    same = gen._bundle_section([], [], "base", "2026-07-13", "headsha99", "headsha99", "main")
    check("bundle: no warning when in sync", "not deployed truth" not in same)


def test_repo_sentinel() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        check("sentinel: bare dir (vault-like) rejected", not _is_code_repo(tmp))
        (root / "cli-tools").mkdir()
        (root / "cli-tools" / "pyproject.toml").write_text("[project]\n")
        check("sentinel: code-repo markers accepted", _is_code_repo(tmp))


def test_window_cap() -> None:
    check("cap: 5-week inherited window flagged", _window_too_wide("2026-06-08", "2026-07-14"))
    check("cap: same-week window fine", not _window_too_wide("2026-07-13", "2026-07-14"))
    check("cap: garbage dates fail open", not _window_too_wide("", "2026-07-14"))


def main() -> int:
    for test in (
        test_git_truth_survives_gh_outage,
        test_gh_enriches_matching_numbers,
        test_gh_overshoot_trimmed_by_timestamps,
        test_gh_only_gated_by_merge_commit_membership,
        test_pr_num_anchored_shapes_only,
        test_bundle_never_drops_pr_numbered_commits,
        test_bundle_labels_checkout_and_divergence,
        test_repo_sentinel,
        test_window_cap,
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
