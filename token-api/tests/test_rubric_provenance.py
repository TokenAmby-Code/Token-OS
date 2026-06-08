"""Tests for derived rubric subconditions + diagnostic provenance.

`sanguinius_satisfied` and `commentary_resolved` are DERIVED in evaluate_rubric
from canonical frontmatter surfaces (the beautifier `<persona>_is` state and the
`commentary` field), NOT from their literal rubric values. The victory-ack
"false-negative" chased in the GT proof was exactly this: a literal
`sanguinius_satisfied: true` on disk has no effect while the derived source
(`sanguinius_is`) is not terminal, so the rubric correctly reports it unmet.

These tests pin that derivation and the human-facing provenance that
describe_rubric / explain_unmet surface so the derivation can never again be
mistaken for a stale/cached read.
"""

from session_doc_helpers import (
    BEAUTIFIER_TERMINAL_INT,
    describe_rubric,
    evaluate_rubric,
    explain_unmet,
)


def test_sanguinius_satisfied_derived_ignores_literal_true():
    # Literal claims satisfied, but the beautifier state is not terminal.
    fm = {
        "victory": {"sanguinius_satisfied": True},
        "sanguinius_is": "hovering at your shoulder",  # resolves to 2 (not terminal)
    }
    status = evaluate_rubric(fm)
    assert status.complete is False
    assert "sanguinius_satisfied" in status.missing
    # The effective rubric value was recomputed to False (literal overwritten).
    assert status.rubric["sanguinius_satisfied"] is False


def test_sanguinius_satisfied_true_when_state_terminal():
    fm = {
        "victory": {"sanguinius_satisfied": False},  # literal False...
        "sanguinius_is": "folding my wings",  # ...but terminal state wins
    }
    status = evaluate_rubric(fm)
    assert status.complete is True
    assert status.missing == []
    assert status.rubric["sanguinius_satisfied"] is True


def test_coderabbit_passed_is_literal_not_derived():
    # A literal flip on a non-derived key takes effect immediately — the
    # contrast that made the GT symptom confusing (cr flip worked, sang did not).
    fm = {
        "victory": {"coderabbit_passed": True, "sanguinius_satisfied": True},
        "sanguinius_is": "folding my wings",
    }
    assert evaluate_rubric(fm).complete is True
    fm["victory"]["coderabbit_passed"] = False
    assert "coderabbit_passed" in evaluate_rubric(fm).missing


def test_describe_rubric_marks_derived_fields_with_provenance():
    fm = {
        "victory": {"coderabbit_passed": True, "sanguinius_satisfied": True},
        "sanguinius_is": "hovering at your shoulder",
    }
    diag = describe_rubric(fm)
    assert diag["complete"] is False
    fields = diag["fields"]
    # Literal field: not derived.
    assert fields["coderabbit_passed"]["derived"] is False
    # Derived field: flagged, effective value recomputed to False, provenance present.
    sang = fields["sanguinius_satisfied"]
    assert sang["derived"] is True
    assert sang["value"] is False
    assert sang["unmet"] is True
    assert sang["derived_from"] == "sanguinius_is"
    assert sang["resolved_int"] == 2
    assert sang["terminal_int"] == BEAUTIFIER_TERMINAL_INT
    assert "no effect" in sang["detail"]


def test_explain_unmet_surfaces_derived_reason():
    fm = {"victory": {"sanguinius_satisfied": True}, "sanguinius_is": "at the easel"}
    status = evaluate_rubric(fm)
    unmet = explain_unmet(fm, status.missing)
    assert len(unmet) == 1
    assert unmet[0]["field"] == "sanguinius_satisfied"
    assert unmet[0]["derived"] is True
    assert "sanguinius_is" in unmet[0]["detail"]


def test_commentary_resolved_derived_from_commentary_field():
    # commentary set -> commentary_resolved derives False regardless of literal.
    fm = {
        "victory": {"commentary_resolved": True},
        "commentary": "please fix the header",
    }
    status = evaluate_rubric(fm)
    assert "commentary_resolved" in status.missing
    diag = describe_rubric(fm)
    assert diag["fields"]["commentary_resolved"]["derived"] is True
    assert diag["fields"]["commentary_resolved"]["value"] is False
    # Clearing commentary satisfies it.
    fm["commentary"] = None
    assert evaluate_rubric(fm).complete is True


def test_skipped_derived_field_not_missing():
    # Marking a derived condition inapplicable via <rubric>_skip drops it.
    fm = {
        "victory": {"sanguinius_satisfied": True},
        "sanguinius_is": "at the easel",
        "victory_skip": ["sanguinius_satisfied"],
    }
    status = evaluate_rubric(fm)
    assert status.complete is True
    assert "sanguinius_satisfied" in status.skipped
    # describe_rubric reflects the skip too.
    assert describe_rubric(fm)["fields"]["sanguinius_satisfied"]["skipped"] is True


def test_literal_only_rubric_has_no_derived_fields():
    # A rubric that declares none of the derived keys exposes no provenance noise.
    fm = {"victory": {"committed": True, "pushed": False}}
    diag = describe_rubric(fm)
    assert diag["fields"]["committed"]["derived"] is False
    assert diag["fields"]["pushed"]["derived"] is False
    assert diag["missing"] == ["pushed"]
