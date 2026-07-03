from __future__ import annotations

import asyncio
import logging
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import talk  # noqa: E402


def test_lookup_instance_for_pane_returns_ps_live_pseudo_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pane = "%900402"

    async def no_instance_id(pane_id: str) -> None:
        assert pane_id == pane
        return None

    async def codex_agent(pane_id: str) -> str:
        assert pane_id == pane
        return "codex"

    async def fake_tmux_list_panes() -> list[dict[str, str]]:
        return [
            {
                "pane_id": pane,
                "position_id": "stack:worker-1",
                "session": "fake",
                "window_index": "9",
                "window_name": "stack",
            }
        ]

    monkeypatch.setattr(talk, "instance_id_for_pane", no_instance_id)
    monkeypatch.setattr(talk, "_resolve_agent_for_pane", codex_agent)
    monkeypatch.setattr(talk, "_tmux_list_panes", fake_tmux_list_panes)

    result = asyncio.run(talk.lookup_instance_for_pane(pane))

    assert result is not None
    assert result["id"] is None
    assert result["engine"] == "codex"
    assert result["tmux_pane"] == pane
    assert result["pane_label"] == "stack:worker-1"
    assert result["rowless_live"] is True


def test_resolve_pane_falls_back_to_tmuxctl_resolver_when_scan_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public singleton routing must not collapse to not_delivered on a scan miss.

    Incident regression: registry/public-name lookup missed a live Custodes pane,
    while the raw pane id was deliverable.  If Token-API's first pane scan returns
    no rows, fall through to tmuxctld's native resolver/scan before declaring the
    target unresolved.
    """

    async def empty_scan() -> list[dict[str, str]]:
        return []

    async def tmuxctl_resolve(target: str | None) -> str | None:
        return "%78" if target == "council:custodes" else None

    monkeypatch.setattr(talk, "_tmux_list_panes", empty_scan)
    monkeypatch.setattr(talk.shared, "resolve_tmux_pane_id", tmuxctl_resolve)

    assert asyncio.run(talk.resolve_pane("council:custodes")) == "%78"


def test_resolve_pane_does_not_accept_non_physical_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def empty_scan() -> list[dict[str, str]]:
        return []

    async def tmuxctl_resolve(_target: str | None) -> str | None:
        return "council:custodes"

    monkeypatch.setattr(talk, "_tmux_list_panes", empty_scan)
    monkeypatch.setattr(talk.shared, "resolve_tmux_pane_id", tmuxctl_resolve)

    assert asyncio.run(talk.resolve_pane("council:custodes")) is None


def test_resolve_pane_logs_tmuxctl_resolver_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def empty_scan() -> list[dict[str, str]]:
        return []

    async def tmuxctl_resolve(_target: str | None) -> str | None:
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr(talk, "_tmux_list_panes", empty_scan)
    monkeypatch.setattr(talk.shared, "resolve_tmux_pane_id", tmuxctl_resolve)

    with caplog.at_level(logging.WARNING, logger=talk.log.name):
        assert asyncio.run(talk.resolve_pane("council:custodes")) is None
    assert "tmuxctl fallback pane resolution failed" in caplog.text
