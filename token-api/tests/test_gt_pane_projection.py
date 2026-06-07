"""Phase 1 Part B — Golden Throne countdown projected onto the pane as @GT_FIRE.

The pane border renders the GT countdown in-format from @GT_FIRE (absolute fire
epoch) via strftime %s + #{e|} integer math — zero fork per redraw. These tests
cover the server side: _gt_push_fire / _gt_clear_fire deliver set-option / unset
to the LIVE-resolved pane and fail closed, and schedule_golden_throne_followup
pushes on a successful arm and clears on the zealotry-below-threshold early return.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def _capture_offloop(main: Any, monkeypatch: Any) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def _fake_offloop(cmd, **kwargs):
        calls.append(tuple(cmd))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(main, "_run_subprocess_offloop", _fake_offloop)
    return calls


def _resolve_to(
    main: Any, monkeypatch: Any, pane: str | None, role: str | None = "palace:N"
) -> None:
    async def _resolve(_instance_id):
        return (pane, role)

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve)


# ---- _gt_push_fire / _gt_clear_fire deliver to the live-resolved pane --------


async def test_push_fire_sets_epoch_on_resolved_pane(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    _resolve_to(main, monkeypatch, "%7")
    calls = _capture_offloop(main, monkeypatch)

    await main._gt_push_fire("inst-1", 1780000000)

    assert calls == [("tmux", "set-option", "-p", "-t", "%7", "@GT_FIRE", "1780000000")]


async def test_clear_fire_unsets_on_resolved_pane(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    _resolve_to(main, monkeypatch, "%7")
    calls = _capture_offloop(main, monkeypatch)

    await main._gt_clear_fire("inst-1")

    assert calls == [("tmux", "set-option", "-p", "-u", "-t", "%7", "@GT_FIRE")]


async def test_push_and_clear_fail_closed_when_pane_unresolved(
    app_env: Any, monkeypatch: Any
) -> None:
    main = app_env.main
    _resolve_to(main, monkeypatch, None, None)
    calls = _capture_offloop(main, monkeypatch)

    await main._gt_push_fire("inst-gone", 1780000000)
    await main._gt_clear_fire("inst-gone")

    assert calls == [], "a vanished pane must get neither a set nor an unset"


# ---- schedule_golden_throne_followup arms → push, drops → clear -------------


async def test_schedule_pushes_gt_fire_on_arm(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    monkeypatch.setattr(main.shared, "get_quiet_hours_status", lambda: {"active": False})
    monkeypatch.setattr(main.scheduler, "add_job", lambda *a, **k: None)
    pushed: list[tuple[str, int]] = []

    async def _cap_push(instance_id, epoch):
        pushed.append((instance_id, epoch))

    monkeypatch.setattr(main, "_gt_push_fire", _cap_push)

    instance = {"id": "inst-gt", "instance_type": "golden_throne", "zealotry": 4}
    result = await main.schedule_golden_throne_followup(instance)

    assert result["scheduled"] is True
    assert len(pushed) == 1
    instance_id, epoch = pushed[0]
    assert instance_id == "inst-gt"
    # Epoch must match the scheduled fire_at the engine reported.
    from datetime import datetime

    assert epoch == int(datetime.fromisoformat(result["fire_at"]).timestamp())


async def test_schedule_clears_gt_fire_below_threshold(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    cleared: list[str] = []

    async def _cap_clear(instance_id):
        cleared.append(instance_id)

    monkeypatch.setattr(main, "_gt_clear_fire", _cap_clear)

    instance = {"id": "inst-low", "instance_type": "golden_throne", "zealotry": 3}
    result = await main.schedule_golden_throne_followup(instance)

    assert result["scheduled"] is False
    assert result["reason"] == "zealotry_below_threshold"
    assert cleared == ["inst-low"]
