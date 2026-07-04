from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path


def load_macrodroid_import():
    script = Path(__file__).resolve().parents[1] / "bin" / "macrodroid-import"
    loader = importlib.machinery.SourceFileLoader("macrodroid_import_bin", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_default_import_result_sets_wiped_false_for_normal_add() -> None:
    module = load_macrodroid_import()

    success, wiped = module.evaluate_import_result(
        replace=False,
        allow_existing=False,
        before_count=40,
        before_match_count=0,
        before_hash="before",
        after_count=41,
        after_match_count=1,
        after_hash="after",
    )

    assert success is True
    assert wiped is False


def test_allow_existing_accepts_one_added_duplicate() -> None:
    module = load_macrodroid_import()

    success, wiped = module.evaluate_import_result(
        replace=False,
        allow_existing=True,
        before_count=41,
        before_match_count=1,
        before_hash="before",
        after_count=42,
        after_match_count=2,
        after_hash="after",
    )

    assert success is True
    assert wiped is False


def test_replace_can_reduce_total_when_duplicates_are_cleaned() -> None:
    module = load_macrodroid_import()

    success, wiped = module.evaluate_import_result(
        replace=True,
        allow_existing=False,
        before_count=42,
        before_match_count=2,
        before_hash="before",
        after_count=41,
        after_match_count=1,
        after_hash="after",
    )

    assert success is True
    assert wiped is False
