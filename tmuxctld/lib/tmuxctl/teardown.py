"""Unified pane-class teardown router — ONE decision, two entry points.

Both teardown entry points consult this single dispatcher:

* WrapperEnd  — a wrapped agent exited cleanly (``/hooks/wrapperend``).
* pane-died   — a pane died/crashed (the global tmux ``pane-died`` hook -> ``/event``).

The pane's CLASS, not the agent that happened to occupy it, selects the action.
There are exactly THREE classes and THREE actions:

* ``PERPETUAL`` — a persona singleton seat (``council:custodes`` ...). Never torn
  down here; the caller REVIVES it (``tmux-pane-respawn`` -> tmuxctld reseat).
* ``SLOT`` — a pre-allocated palace/somnium window pane. CLEARED IN PLACE: the
  runtime stamps + statusline are scrubbed (the #483 ``clear_runtime`` primitive)
  and, if the husk is dead, its shell is revived in place. The pane is PRESERVED
  and returns to the freelist. A slot is NEVER culled: palace ``W/N/S/E`` and
  somnium ``W/N/S/NE/SE`` keep their full fixed pane set after ANY teardown. This
  is the invariant the morning over-reap violated (a completed ``palace:N`` worker
  culled its own slot; a later ``close-pane`` returned "pane target not found").
* ``WORKER`` — a dynamically-created pane OUTSIDE the fixed windows (e.g. a
  mechanicus-stack worker). CULLED via the dead-husk kill path.

Authority (locked Q1 ruling): window membership decides SLOT vs WORKER — a pane
inside a pre-allocated palace/somnium window is a SLOT, a dynamically-created pane
outside those windows is a WORKER. A persona singleton label decides PERPETUAL.
"""

from __future__ import annotations

from typing import Any

from .enums import PaneClass
from .singleton_labels import is_persona_singleton_label
from .tmux_adapter import TmuxAdapter

# The pre-allocated, fixed-pane-set windows whose panes are SLOTs. A slot exit
# only ever clears in place; its pane is structural and must survive teardown.
SLOT_WINDOWS = frozenset({"palace", "somnium"})

__all__ = [
    "SLOT_WINDOWS",
    "PaneClass",
    "apply_teardown",
    "classify_pane",
    "clear_slot_in_place",
    "cull_worker",
    "window_base",
]


def window_base(window_name: str | None) -> str:
    """The base window name with the ``(page)`` suffix stripped (``palace(2)`` -> ``palace``)."""
    return (window_name or "").split("(", 1)[0].strip()


def classify_pane(pane_label: str, window_name: str) -> PaneClass:
    """Resolve a pane to its teardown CLASS from its durable identity.

    Persona singleton label -> PERPETUAL. Otherwise window membership is the
    authority: a pane in a pre-allocated palace/somnium window is a SLOT; any
    pane outside those windows is a dynamically-created WORKER.
    """
    if is_persona_singleton_label(pane_label):
        return PaneClass.PERPETUAL
    if window_base(window_name) in SLOT_WINDOWS:
        return PaneClass.SLOT
    return PaneClass.WORKER


def clear_slot_in_place(
    adapter: TmuxAdapter,
    pane_id: str,
    *,
    pane_role: str = "",
    runtime_already_cleared: bool = False,
) -> dict[str, Any]:
    """Scrub a pre-allocated slot's runtime IN PLACE — never kill the pane.

    ``clear_runtime_state`` drops the runtime stamps and the statusline chrome
    while preserving the durable slot identity (``@PANE_ID`` / ``@PANE_TYPE``). If
    the slot husk is dead (remain-on-exit), its shell is revived in place so the
    slot is once again a live, dispatch-ready freelist pane. ``respawn-pane`` runs WITHOUT ``-k``: the pane is already dead,
    and the adapter's respawn pre-flight re-scrubs the runtime so the revived
    shell never inherits the prior occupant's stamps.
    """
    if not runtime_already_cleared:
        adapter.clear_runtime_state(pane_id)
    revived = False
    if _pane_dead(adapter, pane_id):
        adapter.run("respawn-pane", "-t", pane_id, allow_failure=True)
        revived = True
    return {
        "status": "cleared_in_place",
        "pane_class": PaneClass.SLOT.value,
        "pane": pane_id,
        "pane_role": pane_role,
        "revived": revived,
    }


def cull_worker(
    adapter: TmuxAdapter,
    pane_id: str,
    *,
    pane_role: str = "",
    runtime_already_cleared: bool = False,
) -> dict[str, Any]:
    """Cull a dynamically-created worker pane via the dead-husk kill path.

    The runtime is scrubbed, then the dead husk is reaped. ``reap_dead_husk`` is
    single-pane and liveness-guarded: it only kills a pane tmux reports as dead,
    so a still-live worker (wrapper not yet exited) is left for the subsequent
    ``pane-died`` event — never a collateral kill of an adjacent live pane.
    """
    from .close import reap_dead_husk

    if not runtime_already_cleared:
        adapter.clear_runtime_state(pane_id)
    # The pure reap_dead_husk contract is returned verbatim (the pane CLASS rides
    # the teardown_pane envelope, not this dict, to keep the reap shape stable).
    return reap_dead_husk(adapter, pane_id, pane_role=pane_role)


def apply_teardown(
    adapter: TmuxAdapter,
    pane_id: str,
    pane_class: PaneClass,
    *,
    pane_role: str = "",
    runtime_already_cleared: bool = False,
) -> dict[str, Any]:
    """Run the action for ``pane_class``. PERPETUAL is preserved (caller revives)."""
    if pane_class is PaneClass.SLOT:
        return clear_slot_in_place(
            adapter, pane_id, pane_role=pane_role, runtime_already_cleared=runtime_already_cleared
        )
    if pane_class is PaneClass.WORKER:
        return cull_worker(
            adapter, pane_id, pane_role=pane_role, runtime_already_cleared=runtime_already_cleared
        )
    return {
        "status": "preserved",
        "pane_class": PaneClass.PERPETUAL.value,
        "pane": pane_id,
        "pane_role": pane_role,
    }


def _pane_dead(adapter: TmuxAdapter, pane_id: str) -> bool:
    from .close import pane_dead

    return pane_dead(adapter, pane_id)
