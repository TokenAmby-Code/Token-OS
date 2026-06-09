from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import tmuxctl.skill_invoke as skill_invoke
from tmuxctl.enums import InstanceStatus
from tmuxctl.models import InstanceRegistryEntry, InstanceRegistrySnapshot
from tmuxctl.skill_invoke import (
    invoke_skill_in_pane,
    normalize_skill_name,
    skill_invocation_text,
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
        if args == ("display-message", "-t", "%42", "-p", "#{pane_tty}"):
            return "/dev/ttys999\n"
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        self.calls.append(("show-options", "-pv", "-t", pane_id, option))
        return self.options.get((pane_id, option), "")


def test_skill_invocation_text_prunes_existing_leader_and_uses_codex_dollar():
    assert skill_invocation_text("/preplan", "codex") == "$preplan "
    assert skill_invocation_text("$preplan", "openai") == "$preplan "


def test_skill_invocation_text_uses_claude_slash():
    assert skill_invocation_text("$preplan", "claude") == "/preplan "
    assert skill_invocation_text("preplan", "auto") == "/preplan "


def test_normalize_skill_name_rejects_empty_or_spaced():
    with pytest.raises(ValueError):
        normalize_skill_name("/$")
    with pytest.raises(ValueError):
        normalize_skill_name("pre plan")


def test_invoke_skill_in_pane_inserts_at_prompt_start_with_harness_prefix(monkeypatch):
    adapter = RecordingAdapter()
    monkeypatch.setattr(skill_invoke.time, "sleep", lambda _: None)

    text = invoke_skill_in_pane(adapter, "%42", "/preplan", agent="codex")

    assert text == "$preplan "
    assert adapter.calls[:51] == [("send-keys", "-t", "%42", "PgUp")] * 50 + [
        ("send-keys", "-t", "%42", "Home")
    ]
    assert ("send-keys", "-t", "%42", "-l", "$preplan ") in adapter.calls
    assert adapter.calls[-1] == ("send-keys", "-t", "%42", "End")


def test_resolve_agent_for_pane_uses_registry_engine(monkeypatch):
    adapter = RecordingAdapter()
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(
            InstanceRegistryEntry(
                instance_id="i1",
                device_id="Mac-Mini",
                pane_label="somnium:N",
                tmux_pane="%42",
                working_dir="/tmp",
                status=InstanceStatus.IDLE,
                pre_stop_status=InstanceStatus.UNKNOWN,
                engine="codex",
            ),
        ),
    )
    monkeypatch.setattr(skill_invoke, "fetch_instance_registry", lambda: registry)

    assert skill_invoke.resolve_agent_for_pane(adapter, "%42") == "codex"


def test_resolve_agent_for_pane_prefers_live_process_over_stopped_stale_row(monkeypatch):
    adapter = RecordingAdapter()
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(
            InstanceRegistryEntry(
                instance_id="i1",
                device_id="Mac-Mini",
                pane_label="palace:E",
                tmux_pane="%42",
                working_dir="/tmp",
                status=InstanceStatus.STOPPED,
                pre_stop_status=InstanceStatus.IDLE,
                engine="claude",
            ),
        ),
    )
    monkeypatch.setattr(skill_invoke, "fetch_instance_registry", lambda: registry)

    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["ps", "-t", "ttys999"]
        return subprocess.CompletedProcess(cmd, 0, "node /opt/homebrew/bin/codex\n", "")

    monkeypatch.setattr(skill_invoke.subprocess, "run", fake_run)

    assert skill_invoke.resolve_agent_for_pane(adapter, "%42") == "codex"


def test_resolve_agent_for_pane_falls_back_to_pane_hint(monkeypatch):
    adapter = RecordingAdapter()
    adapter.options[("%42", "@PLANNING_AGENT")] = "codex"
    monkeypatch.setattr(
        skill_invoke, "fetch_instance_registry", lambda: (_ for _ in ()).throw(RuntimeError("down"))
    )

    assert skill_invoke.resolve_agent_for_pane(adapter, "%42") == "codex"


def test_resolve_agent_for_pane_default_when_inconclusive(monkeypatch):
    adapter = RecordingAdapter()
    monkeypatch.setattr(
        skill_invoke, "fetch_instance_registry", lambda: (_ for _ in ()).throw(RuntimeError("down"))
    )

    def fake_run(cmd, **kwargs):
        # Pane process is a plain shell — no claude/codex marker to detect.
        return subprocess.CompletedProcess(cmd, 0, "bash -l\n", "")

    monkeypatch.setattr(skill_invoke.subprocess, "run", fake_run)

    # No registry, no harness in the pane process, no @PLANNING_AGENT hint: the
    # default decides. tmux-plan-menu passes default="auto" so preplan fails
    # closed instead of inserting a guessed leader.
    assert skill_invoke.resolve_agent_for_pane(adapter, "%42") == "claude"
    assert skill_invoke.resolve_agent_for_pane(adapter, "%42", default="auto") == "auto"


def test_resolve_agent_for_pane_rejects_invalid_default():
    # default must stay inside the claude|codex|auto contract — an arbitrary
    # value must never escape as the resolved harness.
    adapter = RecordingAdapter()
    with pytest.raises(ValueError):
        skill_invoke.resolve_agent_for_pane(adapter, "%42", default="bogus")
