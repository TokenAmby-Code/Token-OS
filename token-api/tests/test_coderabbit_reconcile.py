"""Tests for the pure CodeRabbit reconciler (session_doc_helpers).

``reconcile_coderabbit_comments`` folds fetched CodeRabbit comments into the
session-doc victory rubric as bool keys (so the Golden Throne walker drives the
worker through each one) while keeping all rich data in a sibling array. These
tests exercise the pure function only — no network, no DB, no file I/O.
"""

from session_doc_helpers import (
    CODERABBIT_COMMENTS_FIELD,
    CODERABBIT_NITPICK_KEY,
    CODERABBIT_PASSED_KEY,
    CODERABBIT_REVIEW_STATE_FIELD,
    classify_coderabbit_comment,
    reconcile_coderabbit_comments,
)


def _fm(victory: dict) -> dict:
    return {"rubric_key": "victory", "victory": dict(victory)}


def _inline(cid, body, path="a.py", line=10):
    return {"id": cid, "body": body, "path": path, "line": line, "comment_type": "inline"}


# --- classify ---------------------------------------------------------------


def test_classify_actionable_default():
    assert classify_coderabbit_comment("_⚠️ Potential issue_\n\nnull deref here") == "actionable"
    assert classify_coderabbit_comment("🛠️ Refactor suggestion: extract helper") == "actionable"
    # Unlabeled substantive inline comment defaults to actionable.
    assert classify_coderabbit_comment("Please guard against empty input.") == "actionable"


def test_classify_nitpick_duplicate_outside():
    assert classify_coderabbit_comment("🧹 Nitpick: rename variable") == "nitpick"
    assert classify_coderabbit_comment("♻️ Duplicate comment from earlier review") == "duplicate"
    assert classify_coderabbit_comment("⚠️ Outside diff range: unrelated bug") == "outside_diff"


# --- reconcile: add / batch / dedup -----------------------------------------


def test_actionable_each_keyed_nitpicks_batched():
    fm = _fm({"pr_opened": True, CODERABBIT_PASSED_KEY: False})
    fetched = [
        _inline(111, "Potential issue: fix this"),
        _inline(222, "Nitpick: spacing", path="b.py", line=5),
        _inline(333, "Nitpick: naming", path="c.py", line=7),
    ]
    mut = reconcile_coderabbit_comments(fm, fetched, review_terminal=False)
    victory = mut["victory"]
    # One actionable → its own key; two nitpicks → ONE batch key.
    assert victory["coderabbit_111"] is False
    assert victory[CODERABBIT_NITPICK_KEY] is False
    cr_keys = sorted(k for k in victory if k.startswith("coderabbit_"))
    assert cr_keys == ["coderabbit_111", CODERABBIT_NITPICK_KEY, CODERABBIT_PASSED_KEY]
    # Rich sibling array carries all three comments.
    assert len(mut[CODERABBIT_COMMENTS_FIELD]) == 3
    assert mut[CODERABBIT_REVIEW_STATE_FIELD] == "reviewing"


def test_dedup_by_id_is_idempotent():
    fm = _fm({"pr_opened": True, CODERABBIT_PASSED_KEY: False})
    fetched = [_inline(111, "Potential issue")]
    mut1 = reconcile_coderabbit_comments(fm, fetched, review_terminal=False)
    # Feed the prior victory back in with the same fetch → no churn, no dup keys.
    mut2 = reconcile_coderabbit_comments(_fm(mut1["victory"]), fetched, review_terminal=False)
    assert mut2["victory"] == mut1["victory"]
    assert len(mut2[CODERABBIT_COMMENTS_FIELD]) == 1


def test_duplicate_and_outside_diff_stored_not_keyed():
    fm = _fm({"pr_opened": True, CODERABBIT_PASSED_KEY: False})
    fetched = [
        _inline(1, "♻️ Duplicate comment"),
        _inline(2, "Outside diff range issue"),
    ]
    mut = reconcile_coderabbit_comments(fm, fetched, review_terminal=False)
    # No per-comment keys added for duplicate / outside-diff.
    assert not any(
        k.startswith("coderabbit_") and k != CODERABBIT_PASSED_KEY for k in mut["victory"]
    )
    cats = sorted(c["category"] for c in mut[CODERABBIT_COMMENTS_FIELD])
    assert cats == ["duplicate", "outside_diff"]
    # ...but they are recorded with no rubric key.
    assert all(c["key"] is None for c in mut[CODERABBIT_COMMENTS_FIELD])


def test_summary_comment_stored_never_keyed():
    fm = _fm({"pr_opened": True, CODERABBIT_PASSED_KEY: False})
    fetched = [
        {"id": 9, "body": "## Summary by CodeRabbit", "comment_type": "summary"},
        _inline(111, "Potential issue"),
    ]
    mut = reconcile_coderabbit_comments(fm, fetched, review_terminal=False)
    summary = next(c for c in mut[CODERABBIT_COMMENTS_FIELD] if c["id"] == 9)
    assert summary["category"] == "summary"
    assert summary["key"] is None
    assert "coderabbit_9" not in mut["victory"]


# --- reconcile: worker-flip preservation ------------------------------------


def test_worker_flip_preserved_and_mirrored():
    fm = _fm(
        {
            "pr_opened": True,
            CODERABBIT_PASSED_KEY: False,
            "coderabbit_111": True,  # worker already addressed it
            CODERABBIT_NITPICK_KEY: False,
        }
    )
    fetched = [_inline(111, "Potential issue"), _inline(222, "Nitpick")]
    mut = reconcile_coderabbit_comments(fm, fetched, review_terminal=False)
    # Never reset true → false.
    assert mut["victory"]["coderabbit_111"] is True
    # `addressed` mirrors the rubric value into the entry.
    entry = next(c for c in mut[CODERABBIT_COMMENTS_FIELD] if c["id"] == 111)
    assert entry["addressed"] is True


# --- reconcile: terminal pass / prune ---------------------------------------


def test_terminal_clean_passes_and_prunes():
    fm = _fm(
        {
            "pr_opened": True,
            CODERABBIT_PASSED_KEY: False,
            "coderabbit_111": True,
            CODERABBIT_NITPICK_KEY: True,
        }
    )
    fetched = [_inline(111, "Potential issue"), _inline(222, "Nitpick")]
    mut = reconcile_coderabbit_comments(fm, fetched, review_terminal=True)
    assert mut["victory"][CODERABBIT_PASSED_KEY] is True
    # Per-comment keys pruned once passed.
    assert "coderabbit_111" not in mut["victory"]
    assert CODERABBIT_NITPICK_KEY not in mut["victory"]
    assert mut[CODERABBIT_REVIEW_STATE_FIELD] == "complete"


def test_terminal_unresolved_does_not_pass():
    fm = _fm(
        {
            "pr_opened": True,
            CODERABBIT_PASSED_KEY: False,
            "coderabbit_111": False,  # still unaddressed
        }
    )
    fetched = [_inline(111, "Potential issue")]
    mut = reconcile_coderabbit_comments(fm, fetched, review_terminal=True)
    assert mut["victory"][CODERABBIT_PASSED_KEY] is False
    assert mut["victory"]["coderabbit_111"] is False


def test_terminal_with_no_comments_passes_immediately():
    fm = _fm({"pr_opened": True, CODERABBIT_PASSED_KEY: False})
    mut = reconcile_coderabbit_comments(fm, [], review_terminal=True)
    assert mut["victory"][CODERABBIT_PASSED_KEY] is True
    assert mut[CODERABBIT_REVIEW_STATE_FIELD] == "complete"


def test_post_pass_does_not_rekey():
    # Already passed + pruned; same comments still physically on the PR.
    fm = _fm({"pr_opened": True, CODERABBIT_PASSED_KEY: True})
    fetched = [_inline(111, "Potential issue"), _inline(222, "Nitpick")]
    mut = reconcile_coderabbit_comments(fm, fetched, review_terminal=True)
    assert mut["victory"] == {"pr_opened": True, CODERABBIT_PASSED_KEY: True}


# --- reconcile: review state ------------------------------------------------


def test_non_terminal_empty_is_pending():
    fm = _fm({"pr_opened": True, CODERABBIT_PASSED_KEY: False})
    mut = reconcile_coderabbit_comments(fm, [], review_terminal=False)
    assert mut[CODERABBIT_REVIEW_STATE_FIELD] == "pending"


def test_no_typed_rubric_is_noop():
    # Legacy scalar rubric → reconciler can't safely fold; returns no mutations.
    fm = {"rubric_key": "victory", "victory": "pending"}
    assert reconcile_coderabbit_comments(fm, [_inline(1, "x")], review_terminal=False) == {}


def test_does_not_mutate_input_fm():
    victory = {"pr_opened": True, CODERABBIT_PASSED_KEY: False}
    fm = _fm(victory)
    reconcile_coderabbit_comments(fm, [_inline(111, "Potential issue")], review_terminal=False)
    # Original frontmatter untouched.
    assert fm["victory"] == {"pr_opened": True, CODERABBIT_PASSED_KEY: False}
    assert "coderabbit_111" not in fm["victory"]
