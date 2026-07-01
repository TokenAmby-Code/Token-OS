from __future__ import annotations

import json
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
    def __init__(self, options: dict[str, str] | None = None) -> None:
        self.options: dict[str, str] = dict(options or {})
        self.sets: list[tuple[str, str]] = []

    def run(self, *args: str, timeout: float = 0.5) -> _FakeProc:  # noqa: ARG002
        if args[0] == "show-options":
            return _FakeProc(stdout=self.options.get(args[-1], ""), returncode=0)
        if args[:2] == ("set-option", "-p"):
            self.options[args[-2]] = args[-1]
            self.sets.append((args[-2], args[-1]))
            return _FakeProc()
        if args[:2] == ("set-option", "-pu"):
            self.options.pop(args[-1], None)
            return _FakeProc()
        return _FakeProc()


def _record(tmux: _FakeTmux) -> dict:
    return json.loads(tmux.options[tg.GUARD_JSON_OPTION])


def test_arm_writes_json_human_guard_and_projections() -> None:
    tmux = _FakeTmux()
    result = tg.arm(tmux, "%1", seconds=300, now=1000)

    assert result["kind"] == tg.HUMAN
    assert result["until"] == 1300
    assert _record(tmux) == {"kind": tg.HUMAN, "owner": None, "source": tg.SOURCE, "until": 1300}
    assert tmux.options[tg.GUARD_UNTIL_OPTION] == "1300"
    assert tmux.options[tg.GUARD_KIND_OPTION] == tg.HUMAN
    assert tmux.options[tg.GUARD_MARKER_OPTION] == tg.ON_MARKER


def test_arm_preserves_existing_live_human_deadline() -> None:
    tmux = _FakeTmux()
    tg.arm(tmux, "%1", seconds=300, now=1000)
    result = tg.arm(tmux, "%1", seconds=300, now=1100)

    assert result["until"] == 1300
    assert _record(tmux)["until"] == 1300


def test_pending_writes_json_pending_guard() -> None:
    tmux = _FakeTmux()
    result = tg.pending(tmux, "%1", seconds=15, now=1000)

    assert result["kind"] == tg.PENDING
    assert result["until"] == 1015
    assert _record(tmux)["kind"] == tg.PENDING
    assert tmux.options[tg.GUARD_MARKER_OPTION] == tg.PENDING_MARKER


def test_agent_hold_acquires_owner_token_only_when_clear() -> None:
    tmux = _FakeTmux()
    owner = tg.hold(tmux, "%1", seconds=8, now=1000, owner="req-1")

    assert owner == "req-1"
    assert _record(tmux) == {"kind": tg.AGENT, "owner": "req-1", "source": tg.SOURCE, "until": 1008}
    assert tmux.options[tg.GUARD_KIND_OPTION] == tg.AGENT
    assert tmux.options[tg.GUARD_MARKER_OPTION] == tg.AGENT_MARKER
    assert tg.hold(tmux, "%1", seconds=8, now=1001, owner="req-2") is None
    assert _record(tmux)["owner"] == "req-1"


def test_agent_hold_is_denied_by_human_and_pending_guards() -> None:
    for kind in (tg.HUMAN, tg.PENDING):
        tmux = _FakeTmux()
        tg.write_record(tmux, "%1", kind=kind, until=1300, now=1000)
        assert tg.hold(tmux, "%1", seconds=8, now=1000, owner="agent") is None
        assert _record(tmux)["kind"] == kind


def test_owner_release_requires_matching_token() -> None:
    tmux = _FakeTmux()
    tg.hold(tmux, "%1", seconds=8, now=1000, owner="req-1")

    assert tg.release(tmux, "%1", now=1001, owner="other") is False
    assert _record(tmux)["kind"] == tg.AGENT
    assert tg.release(tmux, "%1", now=1001, owner="req-1") is True
    assert _record(tmux)["kind"] == tg.OFF
    assert tmux.options[tg.GUARD_UNTIL_OPTION] == "0"
    assert tmux.options[tg.GUARD_KIND_OPTION] == tg.OFF
    assert tmux.options[tg.GUARD_MARKER_OPTION] == ""


def test_expiry_reprojects_live_guard_or_clears_expired_guard() -> None:
    tmux = _FakeTmux()
    tg.pending(tmux, "%1", seconds=15, now=1000)

    assert tg.expire_pane(tmux, "%1", now=1005)["kind"] == tg.PENDING
    assert tmux.options[tg.GUARD_KIND_OPTION] == tg.PENDING
    assert tg.expire_pane(tmux, "%1", now=2000)["kind"] == tg.OFF
    assert _record(tmux)["kind"] == tg.OFF
