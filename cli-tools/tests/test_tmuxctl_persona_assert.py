from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import assertions
from tmuxctl.assertions import (
    PERSONA_GUARD_OPTION,
    PersonaSpec,
    _guarded_send_persona_command,
    _observed_row_hash,
    _row_matches_persona,
)


FG_LABEL = "mechanicus:fabricator-general"


def _fg_spec() -> PersonaSpec:
    return PersonaSpec(FG_LABEL, "fabricator-general", "hook_driven", "/tmp/fg.md")


def _custodes_spec() -> PersonaSpec:
    return PersonaSpec("legion:custodes", "custodes", "hook_driven", "/tmp/c.md", sync=True)


def _row(**kw):
    base = dict(
        instance_id="i-1",
        pane_label=FG_LABEL,
        legion="fabricator",
        tab_name="fabricator-general-1",
        instance_type="hook_driven",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FakeAdapter:
    """Stores tmux pane options so the persona guard survives across ticks."""

    def __init__(self) -> None:
        self.options: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args, allow_failure: bool = False) -> str:
        self.calls.append(args)
        if args and args[0] == "set-option":
            # set-option -p -t <pane> <opt> <value>   (set)
            # set-option -pu -t <pane> <opt>           (unset)
            if "-pu" in args:
                self.options.pop(args[-1], None)
            else:
                self.options[args[-2]] = args[-1]
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.options.get(option, "")


# ── _row_matches_persona ─────────────────────────────────────────────────────


def test_fg_matches_on_legion_with_unrelated_tab_name():
    # The original bug: tab_name reflects current work, not identity.
    spec = _fg_spec()
    row = _row(legion="fabricator", tab_name="fg-observed-agents-cutoff")
    assert _row_matches_persona(row, spec) is True


def test_fg_matches_on_tab_fallback_when_legion_missing():
    spec = _fg_spec()
    row = _row(legion="astartes", tab_name="fabricator-general work")
    assert _row_matches_persona(row, spec) is True


def test_fg_fails_when_neither_legion_nor_tab_identify():
    spec = _fg_spec()
    row = _row(legion="astartes", tab_name="needs-name")
    assert _row_matches_persona(row, spec) is False


def test_fg_fails_on_pane_label_mismatch_even_with_right_legion():
    spec = _fg_spec()
    row = _row(legion="fabricator", pane_label="mechanicus:other")
    assert _row_matches_persona(row, spec) is False


def test_custodes_predicate_unchanged():
    spec = _custodes_spec()
    assert _row_matches_persona(_row(legion="custodes", instance_type="sync"), spec) is True
    assert _row_matches_persona(_row(legion="astartes", instance_type="sync"), spec) is False


# ── guardrail ────────────────────────────────────────────────────────────────


def test_guard_sends_once_then_suppresses_unchanged_row():
    adapter = FakeAdapter()
    spec = _fg_spec()
    row = _row(legion="astartes", tab_name="needs-name")  # persistently failing

    with (
        patch.object(assertions, "_send_persona_command", return_value=(True, "sent")) as send,
        patch.object(assertions, "log_event") as log,
    ):
        sent1, _, action1 = _guarded_send_persona_command(adapter, "%27", spec, row)
        sent2, _, action2 = _guarded_send_persona_command(adapter, "%27", spec, row)

    assert (sent1, action1) == (True, "persona_correction_sent")
    assert (sent2, action2) == (False, "persona_correction_suppressed")
    send.assert_called_once()  # the loop self-terminates after the first send
    stuck = [c for c in log.call_args_list if c.args and c.args[0] == "persona_assertion_stuck"]
    assert len(stuck) == 1
    assert stuck[0].kwargs["details"]["attempts"] == 2


def test_guard_allows_resend_after_row_changes():
    adapter = FakeAdapter()
    spec = _fg_spec()

    with (
        patch.object(assertions, "_send_persona_command", return_value=(True, "sent")) as send,
        patch.object(assertions, "log_event"),
    ):
        _guarded_send_persona_command(adapter, "%27", spec, _row(tab_name="state-a", legion="astartes"))
        # Observed state mutated → a fresh attempt is warranted.
        sent2, _, action2 = _guarded_send_persona_command(
            adapter, "%27", spec, _row(tab_name="state-b", legion="astartes")
        )

    assert (sent2, action2) == (True, "persona_correction_sent")
    assert send.call_count == 2


def test_guard_records_distinct_hash_per_observed_row():
    spec = _fg_spec()
    h1 = _observed_row_hash(_row(tab_name="a"), spec)
    h2 = _observed_row_hash(_row(tab_name="b"), spec)
    assert h1 != h2
    assert _observed_row_hash(None, spec) == _observed_row_hash(None, spec)


def test_guard_state_persists_in_pane_option():
    adapter = FakeAdapter()
    spec = _fg_spec()
    row = _row(legion="astartes", tab_name="needs-name")
    with (
        patch.object(assertions, "_send_persona_command", return_value=(True, "sent")),
        patch.object(assertions, "log_event"),
    ):
        _guarded_send_persona_command(adapter, "%27", spec, row)
    assert PERSONA_GUARD_OPTION in adapter.options
