"""Unit tests for the green ``agent`` guard state (daemon send hold).

The ``agent`` state is the daemon-send hold: tmuxctld stamps
``@TYPING_AGENT_UNTIL`` while it works a pane, the pane border shows a GREEN ⌨,
and the universal send gate counts the hold (state-blind) so concurrent sends to
that pane delay behind it. A live human ON/PENDING hold always wins — an agent
hold may only be acquired when the pane is OFF.

These exercise the state-machine functions directly over a fake tmux so no live
tmux server is touched (per the hook-tests-no-live-tmux discipline).
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import tmuxctl.typing_guard_state as tg


class _FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


class _FakeTmux:
    """Records set/unset/run-shell calls; answers show-options from ``options``."""

    def __init__(self, options: dict[str, str] | None = None) -> None:
        self.options: dict[str, str] = dict(options or {})
        self.sets: list[tuple[str, str]] = []
        self.unsets: list[str] = []
        self.run_shells: list[str] = []

    def run(self, *args: str, timeout: float = 0.5) -> _FakeProc:  # noqa: ARG002
        verb = args[0] if args else ""
        if verb == "show-options":
            option = args[-1]
            value = self.options.get(option)
            if value is None:
                return _FakeProc(stdout="", returncode=0)
            return _FakeProc(stdout=f"{value}\n", returncode=0)
        if verb == "set-option":
            if "-pu" in args:
                option = args[-1]
                self.unsets.append(option)
                self.options.pop(option, None)
            else:
                option, value = args[-2], args[-1]
                self.sets.append((option, value))
                self.options[option] = value
            return _FakeProc()
        if verb == "run-shell":
            self.run_shells.append(args[-1])
            return _FakeProc()
        return _FakeProc()


def _guard_value(tmux: _FakeTmux) -> str | None:
    return tmux.options.get(tg.GUARD_OPTION)


def test_marker_for_agent_is_green() -> None:
    assert tg.marker_for("agent") == "#[fg=green,bold]⌨#[default]"
    assert tg.AGENT_MARKER == "#[fg=green,bold]⌨#[default]"


def test_hold_off_pane_acquires_agent_and_publishes_green() -> None:
    tmux = _FakeTmux()
    acquired = tg.hold(tmux, "%1", seconds=8, now=1000)

    assert acquired is True
    assert tmux.options[tg.AGENT_OPTION] == "1008"
    assert _guard_value(tmux) == tg.AGENT_MARKER
    assert tmux.run_shells == []
    assert tg.live_state(tmux, "%1", now=1001) == "agent"


def test_hold_denied_when_human_on_lock_live() -> None:
    tmux = _FakeTmux({tg.LOCK_OPTION: "1300"})
    acquired = tg.hold(tmux, "%1", seconds=8, now=1000)

    assert acquired is False
    assert tg.AGENT_OPTION not in tmux.options, "agent hold must not stomp a live human lock"
    assert tg.live_state(tmux, "%1", now=1000) == "on"


def test_hold_denied_when_human_pending_live() -> None:
    tmux = _FakeTmux({tg.PENDING_OPTION: "1015"})
    acquired = tg.hold(tmux, "%1", seconds=8, now=1000)

    assert acquired is False
    assert tg.AGENT_OPTION not in tmux.options
    assert tg.live_state(tmux, "%1", now=1000) == "pending"


def test_human_on_takes_precedence_over_agent_in_live_state() -> None:
    tmux = _FakeTmux({tg.LOCK_OPTION: "1300", tg.AGENT_OPTION: "1008"})
    assert tg.live_state(tmux, "%1", now=1000) == "on"


def test_release_clears_agent_only_and_republishes_off() -> None:
    tmux = _FakeTmux()
    tg.hold(tmux, "%1", seconds=8, now=1000)
    tg.release(tmux, "%1", now=1001)

    assert tg.AGENT_OPTION not in tmux.options
    assert _guard_value(tmux) == ""
    assert tg.live_state(tmux, "%1", now=1001) == "off"


def test_release_reprojects_human_lock_acquired_during_hold() -> None:
    tmux = _FakeTmux()
    tg.hold(tmux, "%1", seconds=8, now=1000)
    # The Emperor starts typing into the pane mid-hold.
    tmux.options[tg.LOCK_OPTION] = "1300"
    tg.release(tmux, "%1", now=1001)

    assert tg.AGENT_OPTION not in tmux.options
    assert _guard_value(tmux) == tg.ON_MARKER, "a human lock that arrived must be re-projected"


def test_expire_pane_clears_stale_agent_hold() -> None:
    tmux = _FakeTmux({tg.AGENT_OPTION: "1008"})
    tg.expire_pane(tmux, "%1", now=2000)

    assert tg.AGENT_OPTION in tmux.unsets
    assert _guard_value(tmux) == ""


def test_expire_pane_keeps_live_agent_hold() -> None:
    tmux = _FakeTmux({tg.AGENT_OPTION: "1008"})
    tg.expire_pane(tmux, "%1", now=1005)

    assert tmux.options.get(tg.AGENT_OPTION) == "1008"
    assert _guard_value(tmux) == tg.AGENT_MARKER
