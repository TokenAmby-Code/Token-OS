"""Daemon-native, engine-agnostic invocation primitive.

These cover the kind-aware leader policy the Shift+Tab plan menu relies on:

  * SKILLS take the engine-specific leader -- ``$skill`` for Codex, ``/skill`` for
    Claude -- and a Codex skill gets the Tab chip-sink.
  * COMMANDS stay a universal ``/command`` in EVERY harness and never get the
    Codex Tab-sink (there is no skill chip to materialize). A command must not
    even probe the pane for its engine, since the leader is engine-independent.

All assertions run against a recording adapter (fake pane, mocked engine); no
live tmux is ever touched.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import tmuxctl.daemon as daemon
import tmuxctl.skill_invoke as skill_invoke
from tmuxctl.service import TmuxControlPlane
from tmuxctl.skill_invoke import (
    insert_invocation_in_pane,
    invocation_leader,
    invocation_sink_keys,
    invocation_text,
    normalize_invocation_kind,
)
from tmuxctl.tmux_adapter import TmuxAdapter


class RecordingAdapter(TmuxAdapter):
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.options: dict[tuple[str, str], str] = {}

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(args)
        if args == ("display-message", "-p", "#{pane_id}"):
            return "%42\n"
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        self.calls.append(("show-options", "-pv", "-t", pane_id, option))
        return self.options.get((pane_id, option), "")


PGUP_HOME = ("send-keys", "-N", "50", "-t", "%42", "PgUp", "Home")
PGDN_END = ("send-keys", "-N", "50", "-t", "%42", "PgDn", "End")


# --- pure leader / text / sink policy --------------------------------------


def test_invocation_text_skill_uses_engine_leader() -> None:
    assert invocation_text("preplan", "claude", kind="skill") == "/preplan "
    assert invocation_text("preplan", "codex", kind="skill") == "$preplan "


def test_invocation_text_command_is_universal_slash_in_every_engine() -> None:
    assert invocation_text("plan", "claude", kind="command") == "/plan "
    assert invocation_text("plan", "codex", kind="command") == "/plan "
    assert invocation_text("compact", "codex", kind="command") == "/compact "


def test_invocation_text_strips_caller_supplied_leader() -> None:
    assert invocation_text("/plan", "codex", kind="command") == "/plan "
    assert invocation_text("$preplan", "claude", kind="skill") == "/preplan "


def test_invocation_leader_splits_on_kind() -> None:
    assert invocation_leader("codex", kind="skill") == "$"
    assert invocation_leader("codex", kind="command") == "/"
    assert invocation_leader("claude", kind="skill") == "/"
    assert invocation_leader("claude", kind="command") == "/"


def test_invocation_sink_keys_only_for_codex_skills() -> None:
    assert invocation_sink_keys("codex", kind="skill") == ("Tab",)
    assert invocation_sink_keys("codex", kind="command") == ()
    assert invocation_sink_keys("claude", kind="skill") == ()


def test_normalize_invocation_kind() -> None:
    assert normalize_invocation_kind(None) == "skill"
    assert normalize_invocation_kind("") == "skill"
    assert normalize_invocation_kind("COMMAND") == "command"
    with pytest.raises(ValueError):
        normalize_invocation_kind("bogus")


# --- full in-pane insert sequence ------------------------------------------


def test_insert_invocation_codex_skill_inserts_dollar_then_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = RecordingAdapter()
    monkeypatch.setattr(skill_invoke.time, "sleep", lambda _: None)

    result = insert_invocation_in_pane(adapter, "%42", "preplan", agent="codex", kind="skill")

    assert result == {"pane": "%42", "agent": "codex", "kind": "skill", "rendered": "$preplan "}
    assert adapter.calls == [
        PGUP_HOME,
        ("send-keys", "-t", "%42", "-l", " "),
        ("send-keys", "-t", "%42", "Left"),
        ("send-keys", "-t", "%42", "-l", "$preplan"),
        ("send-keys", "-t", "%42", "Tab"),
        PGDN_END,
    ]


def test_insert_invocation_codex_command_stays_slash_and_skips_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = RecordingAdapter()
    monkeypatch.setattr(skill_invoke.time, "sleep", lambda _: None)

    result = insert_invocation_in_pane(adapter, "%42", "plan", agent="codex", kind="command")

    assert result["rendered"] == "/plan "
    assert result["kind"] == "command"
    assert ("send-keys", "-t", "%42", "Tab") not in adapter.calls
    assert adapter.calls == [
        PGUP_HOME,
        ("send-keys", "-t", "%42", "-l", " "),
        ("send-keys", "-t", "%42", "Left"),
        ("send-keys", "-t", "%42", "-l", "/plan"),
        PGDN_END,
    ]


def test_insert_invocation_command_never_resolves_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A command leader is engine-independent, so it must NOT pay the (registry +
    # process-tree) resolve cost or risk a wrong leader -- prove resolve is never
    # called by making it explode.
    adapter = RecordingAdapter()
    monkeypatch.setattr(skill_invoke.time, "sleep", lambda _: None)

    def explode(*_a, **_k):
        raise AssertionError("resolve_agent_for_pane must not run for a command")

    monkeypatch.setattr(skill_invoke, "resolve_agent_for_pane", explode)

    result = insert_invocation_in_pane(adapter, "%42", "compact", agent="auto", kind="command")

    assert result["rendered"] == "/compact "
    assert result["agent"] == "auto"


def test_insert_invocation_skill_resolves_auto_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = RecordingAdapter()
    monkeypatch.setattr(skill_invoke.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        skill_invoke, "resolve_agent_for_pane", lambda _adapter, _pane, _agent, **_k: "codex"
    )

    result = insert_invocation_in_pane(adapter, "%42", "preplan", agent="auto", kind="skill")

    assert result["rendered"] == "$preplan "
    assert result["agent"] == "codex"


# --- daemon route ----------------------------------------------------------


def test_insert_invocation_route_registered() -> None:
    assert ("POST", "/insert-invocation") in daemon.ROUTES


def test_h_insert_invocation_command_keeps_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_invoke.time, "sleep", lambda _: None)
    control = TmuxControlPlane(adapter=RecordingAdapter())

    out = daemon._h_insert_invocation(
        control, {"pane": "%42", "name": "plan", "kind": "command", "agent": "codex"}
    )

    assert out["rendered"] == "/plan "
    assert out["kind"] == "command"
    assert out["status"] == "inserted"


def test_h_insert_invocation_skill_uses_engine_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_invoke.time, "sleep", lambda _: None)
    control = TmuxControlPlane(adapter=RecordingAdapter())

    out = daemon._h_insert_invocation(
        control, {"pane": "%42", "name": "preplan", "kind": "skill", "agent": "codex"}
    )

    assert out["rendered"] == "$preplan "
    assert out["agent"] == "codex"
