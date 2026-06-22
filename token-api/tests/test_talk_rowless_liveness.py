from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import talk  # noqa: E402


async def test_lookup_instance_for_pane_returns_ps_live_pseudo_instance(monkeypatch):
    pane = "%900402"
    monkeypatch.setattr(talk, "instance_id_for_pane", lambda pane_id: _async_return(None))
    monkeypatch.setattr(talk, "_resolve_agent_for_pane", lambda pane_id: _async_return("codex"))
    monkeypatch.setattr(
        talk,
        "_tmux_list_panes",
        lambda: _async_return(
            [
                {
                    "pane_id": pane,
                    "position_id": "stack:worker-1",
                    "session": "fake",
                    "window_index": "9",
                    "window_name": "stack",
                }
            ]
        ),
    )

    result = await talk.lookup_instance_for_pane(pane)

    assert result is not None
    assert result["id"] is None
    assert result["engine"] == "codex"
    assert result["tmux_pane"] == pane
    assert result["pane_label"] == "stack:worker-1"
    assert result["rowless_live"] is True


class _async_return:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def _coro():
            return self.value

        return _coro().__await__()
