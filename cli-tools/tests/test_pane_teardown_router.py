"""Unit tests for the unified pane-class teardown router (``tmuxctl.teardown``).

Pure routing logic over FAKE pane ids + mocked stamps — NEVER live tmux (the
standing rule for hook/teardown tests). Covers the three classes and the
load-bearing invariant: a pre-allocated palace/somnium SLOT is cleared IN PLACE
and PRESERVED (returned to the freelist); a persona seat is preserved for revival;
only a dynamically-created WORKER is culled.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.teardown import (  # noqa: E402
    PaneClass,
    apply_teardown,
    classify_pane,
    window_base,
)
from tmuxctl.tmux_adapter import _target_arg  # noqa: E402

# tmux verbs that MUTATE a pane. Teardown of pane A must aim every one of these at
# A and at A alone — never a sibling (the assassin-by-proxy / cross-pane tint-strip
# class). respawn-pane revives A's own shell in place; it is pane-local too.
_MUTATING_VERBS = {"kill-pane", "respawn-pane", "set-option", "set", "select-pane"}

# The full fixed pane set of each pre-allocated window — these must SURVIVE any
# teardown (a slot exit only clears in place).
PALACE_SLOTS = ("palace:W", "palace:N", "palace:S", "palace:E")
SOMNIUM_SLOTS = ("somnium:W", "somnium:N", "somnium:S", "somnium:NE", "somnium:SE")


class FakeAdapter:
    """Records teardown side effects against a fake pane; never touches tmux."""

    def __init__(self, *, pane_dead: bool = True) -> None:
        self._pane_dead = pane_dead
        self.exists = True
        self.cleared: list[str] = []
        self.calls: list[tuple[str, ...]] = []

    def clear_runtime_state(self, target: str) -> None:
        self.cleared.append(target)

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(tuple(args))
        if args[:1] == ("display-message",) and args and args[-1] == "#{pane_dead}":
            return "1" if self._pane_dead else "0"
        if args[:1] == ("display-message",) and args and args[-1] == "#{pane_id}":
            return args[2] if self.exists else ""
        if args[:2] == ("kill-pane", "-t"):
            self.exists = False
            return ""
        if args[:1] == ("respawn-pane",):
            # respawn revives a dead husk's shell IN PLACE — it never removes it.
            self._pane_dead = False
            return ""
        return ""

    @property
    def killed(self) -> bool:
        return any(c[:1] == ("kill-pane",) for c in self.calls)

    @property
    def respawned(self) -> bool:
        return any(c[:1] == ("respawn-pane",) for c in self.calls)


# -- classification ----------------------------------------------------------


def test_window_base_strips_page_suffix() -> None:
    assert window_base("palace(2)") == "palace"
    assert window_base("somnium") == "somnium"
    assert window_base("") == ""
    assert window_base(None) == ""


def test_persona_label_classifies_perpetual_regardless_of_window() -> None:
    # Persona stamp wins even if the seat happens to live in a slot window.
    assert classify_pane("council:custodes", "council") is PaneClass.PERPETUAL
    assert classify_pane("mechanicus:fabricator-general", "mechanicus") is PaneClass.PERPETUAL


def test_palace_and_somnium_panes_classify_slot() -> None:
    for label in PALACE_SLOTS:
        assert classify_pane(label, "palace") is PaneClass.SLOT
    for label in SOMNIUM_SLOTS:
        assert classify_pane(label, "somnium(1)") is PaneClass.SLOT


def test_pane_outside_fixed_windows_classifies_worker() -> None:
    assert classify_pane("mechanicus:1", "mechanicus") is PaneClass.WORKER
    assert classify_pane("council:true-terminal", "council") is PaneClass.WORKER


# -- actions -----------------------------------------------------------------


def test_slot_dead_husk_cleared_in_place_and_revived_not_culled() -> None:
    adapter = FakeAdapter(pane_dead=True)
    result = apply_teardown(adapter, "%7", PaneClass.SLOT, pane_role="palace:N")

    assert result["status"] == "cleared_in_place"
    assert result["revived"] is True
    assert adapter.cleared == ["%7"]  # stamps/statusline scrubbed exactly once
    assert adapter.respawned is True  # shell revived in place
    assert adapter.killed is False  # the slot is NEVER culled
    assert adapter.exists is True  # the pre-allocated slot survives


def test_slot_live_pane_cleared_without_respawn() -> None:
    adapter = FakeAdapter(pane_dead=False)
    result = apply_teardown(adapter, "%7", PaneClass.SLOT, pane_role="palace:N")

    assert result["status"] == "cleared_in_place"
    assert result["revived"] is False
    assert adapter.cleared == ["%7"]
    assert adapter.respawned is False  # a live shell is not respawned out from under
    assert adapter.killed is False


def test_worker_dead_husk_is_culled() -> None:
    adapter = FakeAdapter(pane_dead=True)
    result = apply_teardown(adapter, "%9", PaneClass.WORKER, pane_role="mechanicus:1")

    assert result["status"] == "killed"
    assert adapter.cleared == ["%9"]
    assert adapter.killed is True


def test_worker_live_pane_is_not_killed_no_collateral_reap() -> None:
    # A still-live worker (wrapper not yet exited) is liveness-guarded: cleared but
    # never killed — the same guard that prevents collateral reap of a live pane.
    adapter = FakeAdapter(pane_dead=False)
    result = apply_teardown(adapter, "%9", PaneClass.WORKER, pane_role="mechanicus:1")

    assert result["status"] == "skipped"
    assert adapter.killed is False


def test_perpetual_pane_is_preserved_untouched() -> None:
    adapter = FakeAdapter(pane_dead=True)
    result = apply_teardown(adapter, "%1", PaneClass.PERPETUAL, pane_role="council:custodes")

    assert result["status"] == "preserved"
    assert adapter.cleared == []  # a persona seat is not scrubbed here
    assert adapter.killed is False


def test_fixed_windows_retain_full_pane_set_after_any_teardown() -> None:
    # The load-bearing invariant: every palace/somnium slot survives a teardown of
    # itself. No slot teardown may ever remove a pane from the fixed set.
    for label in PALACE_SLOTS + SOMNIUM_SLOTS:
        adapter = FakeAdapter(pane_dead=True)
        window = label.split(":", 1)[0]  # palace:N -> palace
        cls = classify_pane(label, window)
        assert cls is PaneClass.SLOT
        apply_teardown(adapter, f"%{label}", cls, pane_role=label)
        assert adapter.killed is False
        assert adapter.exists is True


class MultiPaneAdapter:
    """A fake whole tmux: many panes, each with its own stamps; mutations are
    routed by the explicit target so a cross-pane (assassin-by-proxy) write is
    observable as a stamp change on a pane OTHER than the teardown target."""

    def __init__(self, panes: dict[str, dict[str, str]], *, dead: set[str]) -> None:
        self.panes = {pid: dict(stamps) for pid, stamps in panes.items()}
        self.dead = set(dead)
        self.targets: list[str] = []  # every target a MUTATING verb aimed at

    def clear_runtime_state(self, target: str) -> None:
        self.targets.append(target)
        for opt in list(self.panes.get(target, {})):
            self.panes[target][opt] = ""  # scrub ONLY this pane's stamps

    def run(self, *args: str, allow_failure: bool = False) -> str:
        argv = list(args)
        if argv and argv[-1] == "#{pane_dead}":
            return "1" if _target_arg(argv) in self.dead else "0"
        if argv and argv[-1] == "#{pane_id}":
            t = _target_arg(argv)
            return t if t in self.panes else ""
        verb = argv[0] if argv else ""
        if verb in _MUTATING_VERBS:
            self.targets.append(_target_arg(argv))
        if verb == "kill-pane":
            t = _target_arg(argv)
            self.panes.pop(t, None)
            self.dead.discard(t)
        if verb == "respawn-pane":
            self.dead.discard(_target_arg(argv))
        return ""


def _siblings_snapshot(adapter: MultiPaneAdapter, target: str) -> dict:
    return {pid: dict(st) for pid, st in adapter.panes.items() if pid != target}


def test_slot_teardown_touches_only_its_own_pane_siblings_byte_identical() -> None:
    # SCOPE EXPANSION 4: tearing down one slot must leave EVERY other pane's stamps
    # byte-identical — the somnium-teardown-strips-palace-1:S contamination class.
    panes = {
        "%somniumS": {
            "@INSTANCE_ID": "doomed",
            "@TYPING_GUARD_MARKER": "#[fg=cyan]⌨",
            "@PANE_ID": "somnium:S",
        },
        "%palace1S": {
            "@INSTANCE_ID": "live",
            "@TYPING_GUARD_MARKER": "#[fg=green]⌨",
            "@PANE_ID": "palace:S",
        },
        "%custodes": {"@TYPING_GUARD_MARKER": "#[fg=gold]✠", "@PANE_ID": "council:custodes"},
    }
    adapter = MultiPaneAdapter(panes, dead={"%somniumS"})
    before = _siblings_snapshot(adapter, "%somniumS")

    apply_teardown(adapter, "%somniumS", PaneClass.SLOT, pane_role="somnium:S")

    # Every mutating verb aimed ONLY at the teardown target.
    assert set(adapter.targets) == {"%somniumS"}
    # Every other pane's stamps are untouched — byte-identical.
    assert _siblings_snapshot(adapter, "%somniumS") == before


def test_worker_cull_touches_only_its_own_pane_siblings_byte_identical() -> None:
    panes = {
        "%worker": {
            "@INSTANCE_ID": "doomed",
            "@TYPING_GUARD_MARKER": "#[fg=red]⌨",
            "@PANE_ID": "mechanicus:7",
        },
        "%palace1S": {
            "@INSTANCE_ID": "live",
            "@TYPING_GUARD_MARKER": "#[fg=green]⌨",
            "@PANE_ID": "palace:S",
        },
    }
    adapter = MultiPaneAdapter(panes, dead={"%worker"})
    before = _siblings_snapshot(adapter, "%worker")

    apply_teardown(adapter, "%worker", PaneClass.WORKER, pane_role="mechanicus:7")

    assert set(adapter.targets) == {"%worker"}
    assert _siblings_snapshot(adapter, "%worker") == before
