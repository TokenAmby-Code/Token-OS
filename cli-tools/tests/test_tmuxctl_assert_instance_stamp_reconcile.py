"""@INSTANCE_ID stamp is the single source of truth for assert_instance.

Item #4 of the registry-drift family (sibling of PR #249's DB-layer persona
clobber). Two layers disagreed on what counts as a valid ``(pane, instance)``
stamp:

  * ``resolver.resolve_instance`` / ``shared.instance_id_for_pane`` (the *lenient
    armer*'s reverse bridge) read the pane's live ``@INSTANCE_ID`` stamp — that
    stamp IS the source of truth, set by ``_stamp_instance_id`` at register.
  * ``assert_instance`` (the *strict refuser*) ignored the stamp entirely and
    matched the registry row only by the stored ``tmux_pane`` column. Post-
    extraction that column drifts/empties, so a pane that ``resolve_instance``
    happily returns was REFUSED by ``assert_instance`` (``no_registry_instance``).

Fix under test: ``assert_instance`` resolves the registry row through the same
``@INSTANCE_ID`` stamp, so both layers return the SAME validity verdict for the
same ``(pane, instance)``. Fail-closed parity is preserved: an unstamped pane
with no matching row is refused by BOTH, and a dead runtime is refused by
``assert_instance`` regardless of a lingering stamp (liveness still gates).

Pure-unit: a FAKE pane id, an in-memory registry, a fake adapter. No live tmux.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import assertions
from tmuxctl.assertions import assert_instance
from tmuxctl.enums import InstanceStatus
from tmuxctl.models import InstanceRegistryEntry, InstanceRegistrySnapshot
from tmuxctl.resolver import resolve_instance

# A pane id that does NOT exist on any live tmux server.
_LIVE_PANE = "%900401"
_STAMP = "inst-stamp-xyz"


class StampAdapter:
    """Fake adapter: serves @INSTANCE_ID via show_pane_option AND a list-panes
    scan (so ``resolve_instance`` and ``assert_instance`` read the SAME stamp).
    """

    def __init__(self, *, panes: dict[str, str] | None = None) -> None:
        # panes: {pane_id: instance_stamp}
        self.panes = panes or {}
        self.options: dict[tuple[str, str], str] = {}
        for pane_id, stamp in self.panes.items():
            self.options[(pane_id, "@INSTANCE_ID")] = stamp
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args, allow_failure: bool = False) -> str:
        self.calls.append(args)
        if args[:2] == ("list-panes", "-a"):
            # `-F "#{pane_id}\t#{@INSTANCE_ID}\t#{@PANE_ID}"`
            lines = []
            for pane_id, stamp in self.panes.items():
                role = self.options.get((pane_id, "@PANE_ID"), "")
                lines.append(f"{pane_id}\t{stamp}\t{role}")
            return "\n".join(lines)
        if args and args[0] == "set-option":
            if "-pu" in args:
                self.options.pop((args[args.index("-t") + 1], args[-1]), None)
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.options.get((pane_id, option), "")


def _entry(**kw) -> InstanceRegistryEntry:
    base = dict(
        instance_id=_STAMP,
        device_id="Mac-Mini",
        pane_label="",
        # Deliberately MISMATCHED stored pane: the legacy column drifted/emptied
        # post-extraction. Only the @INSTANCE_ID stamp still points here.
        tmux_pane="%STALE",
        working_dir="/tmp",
        status=InstanceStatus.IDLE,
        pre_stop_status=InstanceStatus.UNKNOWN,
        engine="claude",
        last_activity="2026-06-17T00:00:00",
    )
    base.update(kw)
    return InstanceRegistryEntry(**base)


def _registry(*entries: InstanceRegistryEntry) -> InstanceRegistrySnapshot:
    return InstanceRegistrySnapshot(device_id="Mac-Mini", instances=tuple(entries))


def _assert(adapter, *, registry, runtime_ok=True):
    resolved = SimpleNamespace(pane_id=_LIVE_PANE, pane_role="")
    with (
        patch.object(assertions, "resolve_pane", return_value=resolved),
        patch.object(assertions, "_pane_type", return_value=""),  # structured pane
        patch.object(assertions, "_runtime_has_instance", return_value=runtime_ok),
        patch.object(assertions, "fetch_instance_registry", return_value=registry),
        patch.object(assertions, "log_event"),
    ):
        return assert_instance(adapter, _LIVE_PANE)


def test_assert_instance_accepts_pane_resolved_only_by_stamp():
    """The disagreement: stored tmux_pane is stale, but the @INSTANCE_ID stamp
    points at a live, active registry row. ``resolve_instance`` accepts it, so
    ``assert_instance`` must too. RED today: row matched only by stored pane →
    ``no_registry_instance`` → ok=False.
    """
    adapter = StampAdapter(panes={_LIVE_PANE: _STAMP})
    registry = _registry(_entry())

    # Source-of-truth layer: resolve_instance finds the pane via the stamp.
    resolution = resolve_instance(adapter, _STAMP)
    assert resolution.found and resolution.pane_id == _LIVE_PANE

    # Strict layer must now agree on the SAME (pane, instance).
    result = _assert(adapter, registry=registry)
    assert result["ok"] is True, result
    assert result["reason"] == "live", result


def test_both_layers_refuse_an_unstamped_pane_with_no_row():
    """Fail-closed parity: no stamp, no matching row → BOTH refuse."""
    adapter = StampAdapter(panes={})  # pane carries no @INSTANCE_ID
    registry = _registry(_entry())  # row exists but only its stale stored pane

    resolution = resolve_instance(adapter, _STAMP)
    assert resolution.found is False  # no live pane carries the stamp

    result = _assert(adapter, registry=registry)
    assert result["ok"] is False, result
    assert result["reason"] == "no_registry_instance", result


def test_dead_runtime_refused_even_with_matching_stamp():
    """Liveness still gates: a lingering stamp must not resurrect a dead pane.
    assert_instance fails closed on runtime_ok=False regardless of the stamp.
    """
    adapter = StampAdapter(panes={_LIVE_PANE: _STAMP})
    registry = _registry(_entry(status=InstanceStatus.IDLE))

    result = _assert(adapter, registry=registry, runtime_ok=False)
    assert result["ok"] is False, result
    assert result["reason"] == "no_runtime_instance", result
