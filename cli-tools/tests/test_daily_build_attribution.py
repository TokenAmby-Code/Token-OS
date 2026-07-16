#!/usr/bin/env python3
"""daily-build fix-lane regressions (2026-07-14 trial findings).

Covers: git-truth attribution union (finding 4), gh-window overshoot trim,
bundle never silently drops PR-numbered commits, repo sentinel (finding 2),
window-width cap (finding 3), and checkout-vs-remote head labeling (finding 5).

Run directly: uv run --directory cli-tools python tests/test_daily_build_attribution.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cli_tools.daily_build import git_activity as git  # noqa: E402
from cli_tools.daily_build import review_generator as gen  # noqa: E402
from cli_tools.daily_build.cli import (  # noqa: E402
    _date_arg,
    _github_attribution_zero_warning,
    _is_code_repo,
    _window_too_wide,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILURES.append(name)


_GIT_ORIGINALS = {name: getattr(git, name) for name in ("commit_iso", "web_url", "window_shas")}


def _patch_git(
    iso_by_sha: dict[str, str],
    url: str = "https://github.com/org/repo",
    in_window: set[str] | None = None,
):
    """Stub the subprocess-backed helpers attribute_window leans on.

    main() restores the originals after every test so the module isn't left
    mutated for tests (or imports) that run afterwards in the same process.
    """
    git.commit_iso = lambda repo, sha: iso_by_sha.get(sha, "")  # type: ignore[assignment]
    git.web_url = lambda repo: url  # type: ignore[assignment]
    git.window_shas = lambda repo, base, ref="main": in_window or set()  # type: ignore[assignment]


def _restore_git() -> None:
    for name, fn in _GIT_ORIGINALS.items():
        setattr(git, name, fn)


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


def test_date_arg_validation() -> None:
    check("date arg: valid date passes through", _date_arg("2026-07-14") == "2026-07-14")
    for bad in ("not-a-date", "2026-13-40", "07-14-2026"):
        try:
            _date_arg(bad)
            check(f"date arg: {bad!r} rejected", False, "no error raised")
        except argparse.ArgumentTypeError:
            check(f"date arg: {bad!r} rejected", True)


def test_is_ancestor_gates_divergent_anchor() -> None:
    """A prior-note anchor on a divergent branch must not survive as a base."""

    def run(args: list[str], cwd: str) -> str:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    with tempfile.TemporaryDirectory() as tmp:
        run(["git", "init", "-q", "-b", "main"], tmp)
        run(
            [
                "git",
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "A",
            ],
            tmp,
        )
        run(
            [
                "git",
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "B",
            ],
            tmp,
        )
        b = run(["git", "rev-parse", "HEAD"], tmp)
        run(
            [
                "git",
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "C",
            ],
            tmp,
        )
        c = run(["git", "rev-parse", "HEAD"], tmp)
        run(["git", "checkout", "-q", "-b", "side", b], tmp)
        run(
            [
                "git",
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "D",
            ],
            tmp,
        )
        d = run(["git", "rev-parse", "HEAD"], tmp)

        check("ancestor: B is ancestor of C", git.is_ancestor(tmp, b, c))
        check("ancestor: self counts as ancestor", git.is_ancestor(tmp, c, c))
        check("ancestor: divergent D rejected vs C", not git.is_ancestor(tmp, d, c))
        check("ancestor: empty shas rejected", not git.is_ancestor(tmp, "", c))


def test_gh_slug_resolution_and_repo_flag() -> None:
    """Runtime checkout has no GitHub remote → bare gh resolved no repo, 0 rows.

    _gh_slug order: GH_REPO env > remote-derived > Token-OS default; every
    gh pr-list call must carry the explicit --repo flag.
    """
    orig_run = git._run
    orig_env = os.environ.pop("GH_REPO", None)
    calls: list[list[str]] = []

    def make_run(remote_url: str):
        def fake_run(args: list[str], cwd: str, **kw: object) -> subprocess.CompletedProcess:
            calls.append(args)
            if args[:3] == ["git", "remote", "get-url"]:
                rc = 0 if remote_url else 1
                return subprocess.CompletedProcess(args, rc, stdout=f"{remote_url}\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

        return fake_run

    try:
        os.environ["GH_REPO"] = "envorg/envrepo"
        git._run = make_run("git@github.com:someorg/somerepo.git")  # type: ignore[assignment]
        check("slug: GH_REPO env wins over remote", git._gh_slug("/repo") == "envorg/envrepo")
        del os.environ["GH_REPO"]

        check("slug: ssh remote derived", git._gh_slug("/repo") == "someorg/somerepo")
        git._run = make_run("https://github.com/other/thing.git")  # type: ignore[assignment]
        check("slug: https remote derived", git._gh_slug("/repo") == "other/thing")

        # the live failure shape: only remote is the local CD bare path
        git._run = make_run("/Users/x/runtimes/Token-OS/token-os.git")  # type: ignore[assignment]
        check(
            "slug: local-path remote falls back to default",
            git._gh_slug("/repo") == "TokenAmby-Code/Token-OS",
        )
        check(
            "web_url: never empty on the runtime shape",
            git.web_url("/repo") == "https://github.com/TokenAmby-Code/Token-OS",
        )

        calls.clear()
        git.merged_prs("/repo", "2026-07-14")
        git.open_prs("/repo")
        gh_calls = [c for c in calls if c and c[0] == "gh"]
        check("gh: both pr-list calls issued", len(gh_calls) == 2, f"got {len(gh_calls)}")
        check(
            "gh: every call carries explicit --repo slug",
            all(
                "--repo" in c and c[c.index("--repo") + 1] == "TokenAmby-Code/Token-OS"
                for c in gh_calls
            ),
            str(gh_calls),
        )
    finally:
        git._run = orig_run  # type: ignore[assignment]
        if orig_env is not None:
            os.environ["GH_REPO"] = orig_env


def test_gh_zero_rows_carries_diagnostic() -> None:
    """A zero-row GitHub response must be distinguishable from successful data."""
    orig_run = git._run

    def fake_run(args: list[str], cwd: str, **kw: object) -> subprocess.CompletedProcess:
        if args[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

    try:
        git._run = fake_run  # type: ignore[assignment]
        rows, diagnostic = git.merged_prs_result("/repo", "2026-07-14")
        check("gh zero rows: empty result", rows == [], str(rows))
        check(
            "gh zero rows: diagnostic names attribution query",
            diagnostic == "gh pr list --repo TokenAmby-Code/Token-OS returned 0 merged PR rows",
            str(diagnostic),
        )
    finally:
        git._run = orig_run  # type: ignore[assignment]


def test_expected_gh_zero_rows_warns_loudly() -> None:
    warning = _github_attribution_zero_warning(
        [("abc", "fix: runtime attribution (#730)", 730)],
        [],
        "gh pr list --repo TokenAmby-Code/Token-OS returned 0 merged PR rows",
    )
    check("gh zero rows: CLI warning emitted", warning is not None, str(warning))
    check("gh zero rows: warning declares attribution=0", "attribution=0" in (warning or ""))
    check("gh zero rows: warning includes cause", "returned 0 merged PR rows" in (warning or ""))


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
        test_date_arg_validation,
        test_is_ancestor_gates_divergent_anchor,
        test_gh_slug_resolution_and_repo_flag,
        test_gh_zero_rows_carries_diagnostic,
        test_expected_gh_zero_rows_warns_loudly,
    ):
        print(f"— {test.__name__}")
        try:
            test()
        finally:
            _restore_git()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("\nall green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
