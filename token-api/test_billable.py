"""Unit tests for billable.py — classification + the x/y accrual model."""

import os

from billable import (
    WorkClass,
    accrual_weight,
    classify_work_class,
    trickle_numerator,
)

HOME = os.path.expanduser("~")


class TestClassifyByPath:
    def test_civic_mount_is_billable(self):
        assert classify_work_class("/Volumes/Civic/askcivic.git") == WorkClass.BILLABLE
        assert classify_work_class("/Volumes/Civic") == WorkClass.BILLABLE

    def test_askcivic_worktree_under_home_is_billable(self):
        # ~/worktrees/askCivic sits under HOME but must read billable, not personal.
        wd = os.path.join(HOME, "worktrees", "askCivic", "wt-civic-invariant")
        assert classify_work_class(wd) == WorkClass.BILLABLE

    def test_imperium_is_personal(self):
        assert classify_work_class("/Volumes/Imperium/Token-OS") == WorkClass.PERSONAL
        assert classify_work_class("/Volumes/Imperium/Imperium-ENV") == WorkClass.PERSONAL

    def test_home_root_is_personal(self):
        assert classify_work_class(HOME) == WorkClass.PERSONAL
        assert classify_work_class(os.path.join(HOME, "scratch")) == WorkClass.PERSONAL

    def test_unknown_path(self):
        assert classify_work_class("/tmp/whatever") == WorkClass.UNKNOWN
        assert classify_work_class(None) == WorkClass.UNKNOWN
        assert classify_work_class("") == WorkClass.UNKNOWN

    def test_path_boundary_not_prefix_substring(self):
        # /Volumes/CivicOther must NOT match the /Volumes/Civic billable prefix.
        assert classify_work_class("/Volumes/CivicOther/x") == WorkClass.UNKNOWN


class TestClassifyByLegion:
    def test_civic_legion_is_billable(self):
        assert classify_work_class(None, "civic") == WorkClass.BILLABLE
        assert classify_work_class("/tmp/x", "civic") == WorkClass.BILLABLE

    def test_pax_legion_is_billable(self):
        assert classify_work_class(None, "Pax") == WorkClass.BILLABLE

    def test_personal_legions(self):
        for legion in ("mechanicus", "custodes", "astartes", "fabricator", "administratum"):
            assert classify_work_class(None, legion) == WorkClass.PERSONAL

    def test_civic_legion_beats_personal_path(self):
        # Explicit civic legion on an Imperium checkout still reads on-the-clock.
        assert classify_work_class("/Volumes/Imperium/Token-OS", "civic") == WorkClass.BILLABLE

    def test_billable_path_beats_personal_legion(self):
        # Physically in the Civic repo wins even if mislabeled personal.
        assert classify_work_class("/Volumes/Civic/x", "mechanicus") == WorkClass.BILLABLE


class TestAccrualWeight:
    def test_zero_and_one(self):
        assert accrual_weight(0) == 0.0
        assert accrual_weight(-3) == 0.0
        assert accrual_weight(1) == 1.0

    def test_sublinear_growth(self):
        assert accrual_weight(2) == 2.0
        assert accrual_weight(4) == 3.0
        assert accrual_weight(8) == 4.0

    def test_monotonic_and_diminishing(self):
        weights = [accrual_weight(n) for n in range(1, 17)]
        # strictly increasing
        assert all(b > a for a, b in zip(weights, weights[1:]))
        # diminishing marginal gains
        deltas = [b - a for a, b in zip(weights, weights[1:])]
        assert all(b <= a + 1e-9 for a, b in zip(deltas, deltas[1:]))


class TestTrickleNumerator:
    def test_no_signal(self):
        assert trickle_numerator(0, 0) == 0.0

    def test_pure_work(self):
        assert trickle_numerator(3, 0) == 1.0

    def test_pure_distraction(self):
        assert trickle_numerator(0, 5) == 0.0

    def test_mixed_is_fractional(self):
        # 2 work vs 2 distraction -> 0.5 trickle, not the flat 0:0 of today.
        assert trickle_numerator(2, 2) == 0.5
        assert trickle_numerator(3, 1) == 0.75

    def test_bounded(self):
        assert 0.0 <= trickle_numerator(100, 1) <= 1.0
