from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import assertions
from tmuxctl.assertions import (
    PANE_CLOSE_TRANSIENT_OPTIONS,
    PERSONA_GUARD_OPTION,
    PersonaSpec,
    _clear_pane_overlay,
    _dispatch_args,
    _guarded_note_unregistered,
    _guarded_send_persona_command,
    _observed_row_hash,
    _row_matches_persona,
    persona_spec,
)

FG_LABEL = "mechanicus:fabricator-general"
ADMIN_LABEL = "mechanicus:admin"


def _fg_spec() -> PersonaSpec:
    return PersonaSpec(FG_LABEL, "fabricator-general", "hook_driven", "/tmp/fg.md")


def _custodes_spec() -> PersonaSpec:
    return PersonaSpec("legion:custodes", "custodes", "hook_driven", "/tmp/c.md", sync=True)


def _admin_spec() -> PersonaSpec:
    return PersonaSpec(ADMIN_LABEL, "administratum", "hook_driven", "/tmp/admin.md")


def _admin_row(**kw):
    base = dict(
        instance_id="i-admin",
        pane_label=ADMIN_LABEL,
        legion="mechanicus",
        tab_name="needs-name",
        instance_type="hook_driven",
        primarch="administratum",
    )
    base.update(kw)
    return SimpleNamespace(**base)


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


def test_persona_specs_pin_model_defaults():
    assert persona_spec("legion:custodes").model == "opus"
    assert persona_spec("legion:malcador").model == "fable"
    assert persona_spec("mechanicus:fabricator-general").model == ""
    assert persona_spec("mechanicus:admin").model == "sonnet"


def test_malcador_spec_is_not_sync():
    spec = persona_spec("legion:malcador")
    assert spec.sync is False
    assert spec.persona == "malcador"
    assert spec.session_doc.endswith("Terra/Sessions/malcador.md")


def test_dispatch_args_include_model_when_present():
    args = _dispatch_args(
        "%99",
        {
            "engine": "claude",
            "persona": "administratum",
            "model": "sonnet",
            "session_doc": "/tmp/admin.md",
            "instance_type": "hook_driven",
        },
    )

    assert "--model" in args
    assert args[args.index("--model") + 1] == "sonnet"


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


def test_admin_matches_on_primarch_with_fresh_tab_name():
    # The bug: a freshly SessionStart-registered recorder has tab_name='needs-name'
    # (no 'administratum' substring) yet IS the recorder — keying on primarch must
    # match it so the correction loop never arms before the agent self-names.
    spec = _admin_spec()
    assert _row_matches_persona(_admin_row(tab_name="needs-name"), spec) is True


def test_admin_matches_on_tab_fallback_when_primarch_missing():
    # Rows predating the primarch column still match via the tab-name fallback.
    spec = _admin_spec()
    row = _admin_row(primarch="", tab_name="administratum-state-watch")
    assert _row_matches_persona(row, spec) is True


def test_admin_fails_when_neither_primarch_nor_tab_identify():
    spec = _admin_spec()
    assert _row_matches_persona(_admin_row(primarch="", tab_name="needs-name"), spec) is False


def test_admin_fails_on_pane_label_mismatch_even_with_right_primarch():
    spec = _admin_spec()
    row = _admin_row(pane_label="mechanicus:2", primarch="administratum")
    assert _row_matches_persona(row, spec) is False


def test_admin_hash_busts_on_primarch_change():
    # The guard must notice a primarch flip so a stuck backoff can re-evaluate.
    spec = _admin_spec()
    h_missing = _observed_row_hash(_admin_row(primarch=""), spec)
    h_set = _observed_row_hash(_admin_row(primarch="administratum"), spec)
    assert h_missing != h_set


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
        _guarded_send_persona_command(
            adapter, "%27", spec, _row(tab_name="state-a", legion="astartes")
        )
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


def test_unregistered_note_does_not_inject_persona():
    # The core spam fix: a live persona pane with NO row must NOT inject `/persona`
    # (a no-op for singletons) — it notes the anomaly loudly instead.
    adapter = FakeAdapter()
    spec = _admin_spec()
    with (
        patch.object(assertions, "_send_persona_command", return_value=(True, "sent")) as send,
        patch.object(assertions, "log_event") as log,
    ):
        noted, _, action = _guarded_note_unregistered(adapter, "%11", spec)

    assert (noted, action) == (False, "persona_unregistered_noted")
    send.assert_not_called()  # never injects — that was the Opus-burning bug
    events = [c.args[0] for c in log.call_args_list if c.args]
    assert "persona_unregistered_live_runtime" in events


def test_unregistered_note_suppresses_within_backoff():
    adapter = FakeAdapter()
    spec = _admin_spec()
    with (
        patch.object(assertions, "_send_persona_command", return_value=(True, "sent")) as send,
        patch.object(assertions, "log_event") as log,
    ):
        _, _, action1 = _guarded_note_unregistered(adapter, "%11", spec)
        _, _, action2 = _guarded_note_unregistered(adapter, "%11", spec)

    assert action1 == "persona_unregistered_noted"
    assert action2 == "persona_unregistered_suppressed"
    send.assert_not_called()
    # The loud diagnostic fires once, then backs off — no per-tick event spam.
    emitted = [c.args[0] for c in log.call_args_list if c.args]
    assert emitted.count("persona_unregistered_live_runtime") == 1


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


def test_clear_pane_overlay_removes_close_time_state_but_keeps_identity():
    adapter = FakeAdapter()
    adapter.options.update(
        {
            "@PANE_ID": "palace:N",
            "@PANE_TYPE": "palace",
            "@PANE_LABEL": "needs-name",
            "@INSTANCE_ID": "inst-1",
            "@DISCORD_VOICE_LOCK": "1",
            PERSONA_GUARD_OPTION: "{}",
        }
    )

    _clear_pane_overlay(adapter, "%27")

    for option in PANE_CLOSE_TRANSIENT_OPTIONS:
        assert option not in adapter.options
    assert PERSONA_GUARD_OPTION not in adapter.options
    assert adapter.options["@PANE_ID"] == "palace:N"
    assert adapter.options["@PANE_TYPE"] == "palace"
    assert adapter.options["@PANE_TITLE_SUPPRESS"] == "true"


def test_clear_pane_overlay_preserves_static_persona_guard():
    adapter = FakeAdapter()
    adapter.options.update(
        {
            "@PANE_ID": "legion:custodes",
            "@PANE_TYPE": "legion",
            "@PANE_LABEL": "custodes",
            PERSONA_GUARD_OPTION: "{}",
        }
    )

    _clear_pane_overlay(adapter, "%27")

    assert adapter.options[PERSONA_GUARD_OPTION] == "{}"
    assert "@PANE_LABEL" not in adapter.options
