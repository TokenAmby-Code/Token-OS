from types import SimpleNamespace

from tmuxctl import daemon
from tmuxctl.liveness import LiveTui


def test_pane_live_hides_physical_pane_id(monkeypatch):
    """The daemon's public liveness response must contain canonical pane IDs only."""
    physical_pane = "%99"
    public_pane = "somnium:NE"
    control = SimpleNamespace(adapter=object(), public_pane_id=lambda pane: public_pane)
    monkeypatch.setattr(daemon, "resolve_to_physical", lambda adapter, pane: physical_pane)
    monkeypatch.setattr(
        "tmuxctl.liveness.detect_pane_tui",
        lambda adapter, pane: LiveTui(
            pane_id=physical_pane,
            pane_pid=101,
            agent_pid=202,
            agent_command="codex",
        ),
    )

    result = daemon._h_pane_live(control, {"pane": public_pane})

    assert result == {
        "pane_id": public_pane,
        "pane_pid": 101,
        "agent_pid": 202,
        "agent_command": "codex",
        "live": True,
    }
    assert "physical_pane_id" not in result
