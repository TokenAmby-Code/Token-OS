"""WS1 — core restart correctness: session-pinned resolution + unconditional,
verified persona/perpetual boot.

Two verified defects are guarded here:

1. ``resolve_pane`` chose the snapshot session via the target-less
   ``current_session_name()``. The restart executor runs detached after parking
   clients into ``_stash`` and killing old ``main``, so that ambient read does
   NOT return the rebuilt session and every persona/resume label resolves
   against the wrong session ("pane target not found" for panes that exist).
   The restart path must pin resolution to the explicit rebuilt session.

2. The post-rebuild persona/reservist assertion treated "no exception" as
   success. A seat counts only when the pane actually hosts a live agent.
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.assertions import PERSONA_LABELS
from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.executor import RestartExecutor
from tmuxctl.models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from tmuxctl.resolver import resolve_pane


def _pane(pane_id: str, role: str, window: str = "council") -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=4,
        window_name=window,
        pane_index=0,
        width=120,
        height=40,
        current_command="claude",
        tty="/dev/ttys004",
        pane_role=role,
        grid_state=GridState.SMALL,
        pane_kind=PaneKind.COUNCIL,
        reserved=False,
        active=True,
    )


def _main_workspace() -> WorkspaceSnapshot:
    window = WindowSnapshot(
        session_name="main",
        window_index=4,
        window_name="council",
        archetype=WindowArchetype.COUNCIL,
        focused=False,
        grid_expanded="none",
        grid_stash="",
        side_expanded="none",
        panes=(_pane("%41", "council:custodes"),),
    )
    return WorkspaceSnapshot(session_name="main", windows=(window,))


def _empty_workspace(session: str) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(session_name=session, windows=())


# ── Defect 1: session-pinned resolution after teardown/park ───────────────────


def test_resolve_pane_pins_session_after_teardown(monkeypatch):
    # Simulate the detached executor: the ambient session is the parked _stash,
    # NOT the freshly rebuilt main. A blind resolve snapshots _stash and misses
    # the persona pane that lives in main; a session-pinned resolve finds it.
    snapshots = {"main": _main_workspace(), "_stash": _empty_workspace("_stash")}
    monkeypatch.setattr(
        "tmuxctl.snapshot.build_workspace_snapshot",
        lambda adapter, session: snapshots.get(session, _empty_workspace(session)),
    )

    adapter = SimpleNamespace(
        current_session_name=lambda: "_stash",
        pinned_resolution_session=None,
    )

    # The bug: ambient resolution runs against _stash and fails for a live pane.
    with pytest.raises(ValueError, match="pane target not found"):
        resolve_pane(adapter, "council:custodes")

    # The fix: explicit session pin snapshots main and resolves the pane.
    resolved = resolve_pane(adapter, "council:custodes", session_name="main")
    assert resolved.pane_id == "%41"
    assert resolved.pane_role == "council:custodes"


def test_resolve_pane_honors_adapter_pin(monkeypatch):
    # The resume loop dispatches through the generic run() interception path,
    # which cannot pass an explicit session arg. The executor pins the adapter so
    # that path still resolves against the rebuilt session.
    snapshots = {"main": _main_workspace(), "_stash": _empty_workspace("_stash")}
    monkeypatch.setattr(
        "tmuxctl.snapshot.build_workspace_snapshot",
        lambda adapter, session: snapshots.get(session, _empty_workspace(session)),
    )

    adapter = SimpleNamespace(
        current_session_name=lambda: "_stash",
        pinned_resolution_session="main",
    )

    # No explicit session arg, but the adapter pin routes resolution at main.
    assert resolve_pane(adapter, "council:custodes").pane_id == "%41"

    # Explicit arg still wins over the pin, and an unset pin falls back to ambient.
    adapter.pinned_resolution_session = None
    with pytest.raises(ValueError, match="pane target not found"):
        resolve_pane(adapter, "council:custodes")


def test_pin_resolution_session_context_restores_previous():
    from tmuxctl.tmux_adapter import TmuxAdapter

    adapter = TmuxAdapter.__new__(TmuxAdapter)
    adapter.pinned_resolution_session = None
    with adapter.pin_resolution_session("main"):
        assert adapter.pinned_resolution_session == "main"
    assert adapter.pinned_resolution_session is None


# ── Defect 2: daemon-routed persona seating + live-agent-verified boot ─────────


def _executor() -> RestartExecutor:
    return RestartExecutor(adapter=SimpleNamespace(pinned_resolution_session="main"))


class _FakeResp:
    """Minimal context-manager stand-in for urllib's HTTPResponse."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> bytes:
        return self._text.encode("utf-8")

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _envelope(results: list[dict]) -> str:
    return json.dumps({"ok": True, "result": {"results": results}})


def _ok_results() -> list[dict]:
    return [{"ok": True, "action": "none", "pane_label": label} for label in sorted(PERSONA_LABELS)]


def test_persistent_panes_seat_personas_via_daemon_reconcile():
    # The daemon (tmuxctld) is the SOLE persona launcher: restart seating routes
    # through POST /reconcile, NOT an in-process assert loop. The R2 liveness
    # check still runs read-only, and reservists still seat in-process.
    executor = _executor()
    captured: dict[str, object] = {}
    reservists: list[tuple[str, str | None]] = []

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["method"] = req.get_method()
        captured["body"] = req.data.decode("utf-8")
        return _FakeResp(_envelope(_ok_results()))

    def fake_ensure(label, target, cwd, prompt, session_name=None):
        reservists.append((target, session_name))
        return ""

    with (
        patch("urllib.request.urlopen", fake_urlopen),
        patch.object(executor, "_resolve_optional_pane", return_value="%99"),
        patch.object(executor, "_pane_has_agent_runtime", return_value=True),
        patch.object(executor, "_ensure_reservist_runtime", side_effect=fake_ensure),
    ):
        violations = executor._assert_persistent_runtime_panes("main")

    assert violations == []
    assert str(captured["url"]).endswith("/reconcile")
    assert captured["method"] == "POST"
    assert captured["timeout"] == 20
    assert json.loads(str(captured["body"])) == {"session": "main"}
    # Reservists stay in-process (the daemon has no reservist launcher yet).
    assert {target for target, _ in reservists} == {"reservists:civic", "reservists:token-os"}
    assert all(session == "main" for _, session in reservists)


def test_persistent_panes_loud_violation_when_daemon_unreachable():
    # If tmuxctld is down, persona seating fails LOUDLY — the cold path is gone.
    executor = _executor()

    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with (
        patch("urllib.request.urlopen", boom),
        patch.object(executor, "_resolve_optional_pane", return_value="%99"),
        patch.object(executor, "_pane_has_agent_runtime", return_value=True),
        patch.object(executor, "_ensure_reservist_runtime", return_value=""),
    ):
        violations = executor._assert_persistent_runtime_panes("main")

    assert any("tmuxctld reconcile unreachable" in v for v in violations)
    assert any("sole persona launcher" in v for v in violations)


def test_persistent_panes_translate_per_seat_daemon_failure():
    # A per-seat daemon failure (ok=false, non-launch action) becomes a violation.
    executor = _executor()
    bad_label = sorted(PERSONA_LABELS)[0]
    results = [
        {"ok": False, "action": "error", "reason": "boom", "pane_label": bad_label}
        if label == bad_label
        else {"ok": True, "action": "none", "pane_label": label}
        for label in sorted(PERSONA_LABELS)
    ]

    def fake_urlopen(req, timeout=None):
        return _FakeResp(_envelope(results))

    with (
        patch("urllib.request.urlopen", fake_urlopen),
        patch.object(executor, "_resolve_optional_pane", return_value="%99"),
        patch.object(executor, "_pane_has_agent_runtime", return_value=True),
        patch.object(executor, "_ensure_reservist_runtime", return_value=""),
    ):
        violations = executor._assert_persistent_runtime_panes("main")

    assert any(f"persistent pane assertion failed for {bad_label}" in v for v in violations)


def test_persistent_panes_flag_persona_without_live_agent():
    # R2: a seat the daemon reports OK but whose pane hosts no live agent is a
    # hard verification failure — not silently accepted.
    executor = _executor()

    def fake_urlopen(req, timeout=None):
        return _FakeResp(_envelope(_ok_results()))

    with (
        patch("urllib.request.urlopen", fake_urlopen),
        patch.object(executor, "_resolve_optional_pane", return_value="%99"),
        patch.object(executor, "_pane_has_agent_runtime", return_value=False),
        patch.object(executor, "_ensure_reservist_runtime", return_value=""),
    ):
        violations = executor._assert_persistent_runtime_panes("main")

    assert len(violations) == len(PERSONA_LABELS)
    assert all("no live agent" in v for v in violations)


def test_persistent_panes_flag_missing_persona_pane():
    # A persona pane absent from the rebuilt session is a verification failure,
    # never a silent pass.
    executor = _executor()

    def fake_urlopen(req, timeout=None):
        return _FakeResp(_envelope(_ok_results()))

    with (
        patch("urllib.request.urlopen", fake_urlopen),
        patch.object(executor, "_resolve_optional_pane", return_value=""),
        patch.object(executor, "_pane_has_agent_runtime", return_value=True),
        patch.object(executor, "_ensure_reservist_runtime", return_value=""),
    ):
        violations = executor._assert_persistent_runtime_panes("main")

    assert len(violations) == len(PERSONA_LABELS)
    assert all("missing after assertion" in v for v in violations)
