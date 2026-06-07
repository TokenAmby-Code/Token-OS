"""Tests for the CodeRabbit fetch + sync-wrapper contracts (main.py).

These cover the two hardening invariants the poller relies on to honour its
"fails soft, never reconciles from partial data" promise:

  - ``_fetch_coderabbit_comments`` fully paginates its list calls and is
    ALL-OR-NOTHING: any failed/incomplete fetch returns an error result with
    empty comments so the reconciler is never fed a truncated view.
  - ``_coderabbit_sync_sync`` waits past the bounded inner gh budget before
    giving up, so a slow pass can't be abandoned mid-flight while the next
    interval starts a second pass racing it on the same frontmatter.

Only the ``_gh_api_json`` seam is mocked — no network, no subprocess.
"""

import pytest

_CR = "coderabbitai[bot]"


def _gh_router(responses: dict, calls: list):
    """Fake ``_gh_api_json`` routing by endpoint and recording every call."""

    async def fake(path_args):
        calls.append(list(path_args))
        ep = path_args[0]
        if "/issues/" in ep and "/comments" in ep:
            return responses.get("issue")
        if "/pulls/" in ep and "/comments" in ep:
            return responses.get("inline")
        if "/commits/" in ep and "/statuses" in ep:
            return responses.get("statuses")
        if "/pulls/" in ep:  # bare PR metadata object
            return responses.get("pr")
        return None

    return fake


def _ok_responses() -> dict:
    return {
        "inline": [
            {
                "id": 1,
                "user": {"login": _CR},
                "body": "Potential issue: guard the null deref",
                "path": "a.py",
                "line": 3,
            },
            {"id": 2, "user": {"login": "a-human"}, "body": "looks fine"},  # filtered
        ],
        "issue": [
            {"id": 9, "user": {"login": _CR}, "body": "## Summary by CodeRabbit"},
        ],
        "pr": {
            "head": {"sha": "abc123"},
            "state": "open",
            "created_at": "2026-06-01T00:00:00Z",
            "merged_at": None,
        },
        "statuses": [
            {"context": "coderabbit", "state": "success", "updated_at": "2026-06-02T00:00:00Z"},
        ],
    }


# --- success / pagination ----------------------------------------------------


async def test_fetch_success_filters_to_bot_and_collects(app_env, monkeypatch):
    main = app_env.main
    calls: list = []
    monkeypatch.setattr(main, "_gh_api_json", _gh_router(_ok_responses(), calls))

    out = await main._fetch_coderabbit_comments("o/r", "5")

    assert out["error"] is None
    assert out["review_terminal"] is True
    # The human comment is filtered; both CodeRabbit comments are kept.
    assert sorted(c["id"] for c in out["comments"]) == [1, 9]
    assert out["pr_state"] == "open"
    assert out["merged"] is False


async def test_fetch_fully_paginates_all_three_list_calls(app_env, monkeypatch):
    main = app_env.main
    calls: list = []
    monkeypatch.setattr(main, "_gh_api_json", _gh_router(_ok_responses(), calls))

    await main._fetch_coderabbit_comments("o/r", "5")

    paginated = [c[0] for c in calls if "--paginate" in c]
    assert any("/pulls/5/comments" in ep for ep in paginated), "inline comments must paginate"
    assert any("/issues/5/comments" in ep for ep in paginated), "issue comments must paginate"
    assert any("/statuses" in ep for ep in paginated), "commit statuses must paginate"
    # The single-object PR metadata call is NOT paginated.
    assert not any("--paginate" in c and c[0].endswith("/pulls/5") for c in calls)


# --- all-or-nothing: any incomplete fetch skips the round --------------------


@pytest.mark.parametrize(
    "broken,reason",
    [
        ("inline", "inline_fetch_failed"),
        ("issue", "issue_fetch_failed"),
        ("statuses", "statuses_fetch_failed"),
    ],
)
async def test_fetch_partial_list_failure_is_error(app_env, monkeypatch, broken, reason):
    main = app_env.main
    responses = _ok_responses()
    responses[broken] = None  # gh call failed → _gh_api_json returns None
    monkeypatch.setattr(main, "_gh_api_json", _gh_router(responses, []))

    out = await main._fetch_coderabbit_comments("o/r", "5")

    assert out["error"] == reason
    assert out["comments"] == []
    assert out["review_terminal"] is False


async def test_fetch_pr_meta_not_dict_is_error(app_env, monkeypatch):
    main = app_env.main
    responses = _ok_responses()
    responses["pr"] = None
    monkeypatch.setattr(main, "_gh_api_json", _gh_router(responses, []))

    out = await main._fetch_coderabbit_comments("o/r", "5")

    assert out["error"] == "pr_meta_failed"
    assert out["comments"] == []


async def test_fetch_missing_head_sha_is_error(app_env, monkeypatch):
    main = app_env.main
    responses = _ok_responses()
    responses["pr"] = {"head": {}, "state": "open"}
    monkeypatch.setattr(main, "_gh_api_json", _gh_router(responses, []))

    out = await main._fetch_coderabbit_comments("o/r", "5")

    assert out["error"] == "pr_meta_no_head_sha"


# --- review_terminal only from a fully-fetched status list -------------------


async def test_review_terminal_false_when_status_pending(app_env, monkeypatch):
    main = app_env.main
    responses = _ok_responses()
    responses["statuses"] = [
        {"context": "coderabbit", "state": "pending", "updated_at": "2026-06-02T00:00:00Z"},
    ]
    monkeypatch.setattr(main, "_gh_api_json", _gh_router(responses, []))

    out = await main._fetch_coderabbit_comments("o/r", "5")

    assert out["error"] is None
    assert out["review_terminal"] is False


# --- sync-wrapper timeout invariant (finding 3483) --------------------------


def test_pass_timeout_exceeds_bounded_gh_budget(app_env):
    main = app_env.main
    # _fetch_coderabbit_comments makes four sequential gh calls, each capped at
    # CODERABBIT_GH_TIMEOUT_SECONDS. The wrapper must wait past that bounded total
    # so it never abandons a still-running pass (which would let the next interval
    # start a second pass racing it on the same frontmatter).
    assert main.CODERABBIT_SYNC_PASS_TIMEOUT_SECONDS > 4 * main.CODERABBIT_GH_TIMEOUT_SECONDS
