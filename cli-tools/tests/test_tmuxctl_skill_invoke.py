from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import tmuxctl.cli as tmuxctl_cli
import tmuxctl.skill_invoke as skill_invoke
from tmuxctl.enums import InstanceStatus
from tmuxctl.models import InstanceRegistryEntry, InstanceRegistrySnapshot
from tmuxctl.service import TmuxControlPlane
from tmuxctl.skill_invoke import (
    codex_skill_sink_keys,
    insert_text,
    invoke_skill_in_pane,
    looks_like_codex_skill_invocation,
    move_to_prompt_end,
    move_to_prompt_start,
    normalize_skill_name,
    send_invocation_to_pane,
    send_skill_invocation_to_pane,
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


def test_skill_invocation_text_appends_arguments_without_trailing_padding():
    assert (
        skill_invocation_text(
            "golden-throne-sop",
            "codex",
            'victory condition "needs tests passing" is unmet',
        )
        == '$golden-throne-sop victory condition "needs tests passing" is unmet'
    )


def test_codex_skill_sink_keys_only_for_codex():
    assert codex_skill_sink_keys("codex") == ("Tab",)
    assert codex_skill_sink_keys("openai") == ("Tab",)
    assert codex_skill_sink_keys("claude") == ()


def test_looks_like_codex_skill_invocation():
    assert looks_like_codex_skill_invocation("$golden-throne-sop victory condition x")
    assert not looks_like_codex_skill_invocation("/golden-throne-sop victory condition x")
    assert not looks_like_codex_skill_invocation("plain $golden-throne-sop")
    assert not looks_like_codex_skill_invocation("$")
    assert not looks_like_codex_skill_invocation("$   ")


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
    # PgUp x50 + Home emitted via tmux repeat-count in ONE send-keys, then the right-side buffer
    # (space, Left) and the rstripped leader, then the codex Tab sink — never a
    # concatenated "$preplanexisting" — then PgDn x50 + End batched into one send.
    pgup_home = ("send-keys", "-N", "50", "-t", "%42", "PgUp", "Home")
    pgdn_end = ("send-keys", "-N", "50", "-t", "%42", "PgDn", "End")
    assert adapter.calls == [
        pgup_home,
        ("send-keys", "-t", "%42", "-l", " "),
        ("send-keys", "-t", "%42", "Left"),
        ("send-keys", "-t", "%42", "-l", "$preplan"),
        ("send-keys", "-t", "%42", "Tab"),
        pgdn_end,
    ]


def test_invoke_skill_in_pane_does_not_tab_sink_claude(monkeypatch):
    adapter = RecordingAdapter()
    monkeypatch.setattr(skill_invoke.time, "sleep", lambda _: None)

    text = invoke_skill_in_pane(adapter, "%42", "preplan", agent="claude")

    assert text == "/preplan "
    pgup_home = ("send-keys", "-N", "50", "-t", "%42", "PgUp", "Home")
    pgdn_end = ("send-keys", "-N", "50", "-t", "%42", "PgDn", "End")
    assert adapter.calls == [
        pgup_home,
        ("send-keys", "-t", "%42", "-l", " "),
        ("send-keys", "-t", "%42", "Left"),
        ("send-keys", "-t", "%42", "-l", "/preplan"),
        pgdn_end,
    ]
    assert ("send-keys", "-t", "%42", "Tab") not in adapter.calls


def test_invoke_skill_in_pane_prompt_start_buffer_separates_arguments(monkeypatch):
    adapter = RecordingAdapter()
    monkeypatch.setattr(skill_invoke.time, "sleep", lambda _: None)

    text = invoke_skill_in_pane(
        adapter,
        "%42",
        "golden-throne-sop",
        agent="codex",
        arguments='victory condition "needs tests passing" is unmet   ',
    )

    assert text == '$golden-throne-sop victory condition "needs tests passing" is unmet'
    typed_literals = [
        call for call in adapter.calls if call[:4] == ("send-keys", "-t", "%42", "-l")
    ]
    assert typed_literals == [
        ("send-keys", "-t", "%42", "-l", " "),
        (
            "send-keys",
            "-t",
            "%42",
            "-l",
            '$golden-throne-sop victory condition "needs tests passing" is unmet',
        ),
    ]
    tab_index = adapter.calls.index(("send-keys", "-t", "%42", "Tab"))
    # PgDn is now batched into the single prompt-end send-keys, not its own call.
    prompt_end_index = next(i for i, call in enumerate(adapter.calls) if "PgDn" in call)
    assert tab_index < prompt_end_index


def test_resolve_agent_detects_codex_in_pane_process_subtree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rowless Codex workers surface as shell/node descendants, not registry rows."""
    adapter = RecordingAdapter()

    def fake_run(*args: str, allow_failure: bool = False) -> str:
        adapter.calls.append(args)
        if args == ("display-message", "-t", "%42", "-p", "#{pane_id}"):
            return "%42\n"
        if args == ("display-message", "-t", "%42", "-p", "#{pane_pid}"):
            return "100\n"
        return ""

    adapter.run = fake_run
    monkeypatch.setattr(
        skill_invoke,
        "fetch_instance_registry",
        lambda: InstanceRegistrySnapshot(device_id="Mac-Mini", instances=()),
    )
    monkeypatch.setattr(
        skill_invoke,
        "_process_tree",
        lambda: ({100: [101], 101: [102]}, {101: "node worker", 102: "node /x/@openai/codex"}),
    )

    assert skill_invoke.resolve_agent_for_pane(adapter, "%42", default="auto") == "codex"


def test_move_to_prompt_start_emits_single_batched_pgup_then_home() -> None:
    # One send-keys subprocess carries tmux repeat-count PgUp plus the Home terminator.
    adapter = RecordingAdapter()
    move_to_prompt_start(adapter, "%42")
    assert adapter.calls == [("send-keys", "-N", "50", "-t", "%42", "PgUp", "Home")]


def test_move_to_prompt_start_honors_page_ups():
    adapter = RecordingAdapter()
    move_to_prompt_start(adapter, "%42", page_ups=3)
    assert adapter.calls == [("send-keys", "-N", "3", "-t", "%42", "PgUp", "Home")]


def test_insert_text_emits_buffer_separated_send():
    # insert_text now preloads a right-side buffer (space, Left) before the
    # rstripped payload so a prepend onto existing composer text can never form a
    # concatenated token — three sends, not one literal.
    adapter = RecordingAdapter()
    insert_text(adapter, "%42", "/plan ")
    assert adapter.calls == [
        ("send-keys", "-t", "%42", "-l", " "),
        ("send-keys", "-t", "%42", "Left"),
        ("send-keys", "-t", "%42", "-l", "/plan"),
    ]


def test_move_to_prompt_end_emits_single_batched_pgdn_then_end() -> None:
    # One send-keys subprocess carries tmux repeat-count PgDn plus the End terminator.
    adapter = RecordingAdapter()
    move_to_prompt_end(adapter, "%42")
    assert adapter.calls == [("send-keys", "-N", "50", "-t", "%42", "PgDn", "End")]


def test_move_to_prompt_end_honors_page_downs():
    adapter = RecordingAdapter()
    move_to_prompt_end(adapter, "%42", page_downs=3)
    assert adapter.calls == [("send-keys", "-N", "3", "-t", "%42", "PgDn", "End")]


def _cli_with_adapter(adapter, monkeypatch):
    """Route tmuxctl.main through a stub adapter so subcommands record their sends."""
    monkeypatch.setattr(tmuxctl_cli, "TmuxControlPlane", lambda: TmuxControlPlane(adapter))


def test_cli_prompt_start_routes_to_adapter(monkeypatch):
    adapter = RecordingAdapter()
    _cli_with_adapter(adapter, monkeypatch)
    assert tmuxctl_cli.main(["prompt-start", "--pane", "%42", "--page-ups", "2"]) == 0
    assert adapter.calls == [("send-keys", "-N", "2", "-t", "%42", "PgUp", "Home")]


def test_cli_insert_text_routes_to_adapter(monkeypatch):
    adapter = RecordingAdapter()
    _cli_with_adapter(adapter, monkeypatch)
    assert tmuxctl_cli.main(["insert-text", "--pane", "%42", "--text", "/compact "]) == 0
    assert adapter.calls == [
        ("send-keys", "-t", "%42", "-l", " "),
        ("send-keys", "-t", "%42", "Left"),
        ("send-keys", "-t", "%42", "-l", "/compact"),
    ]


def test_cli_prompt_end_routes_to_adapter(monkeypatch):
    adapter = RecordingAdapter()
    _cli_with_adapter(adapter, monkeypatch)
    assert tmuxctl_cli.main(["prompt-end", "--pane", "%42", "--page-downs", "2"]) == 0
    assert adapter.calls == [("send-keys", "-N", "2", "-t", "%42", "PgDn", "End")]


def test_send_skill_invocation_to_pane_submits_through_gated_adapter(monkeypatch):
    adapter = RecordingAdapter()
    monkeypatch.setattr(skill_invoke.time, "sleep", lambda _: None)

    text = send_skill_invocation_to_pane(
        adapter,
        "%42",
        "golden-throne-sop",
        agent="codex",
        arguments='victory condition "needs tests passing" is unmet',
    )

    assert text == '$golden-throne-sop victory condition "needs tests passing" is unmet'
    assert ("send-keys", "-t", "%42", "-l", text) in adapter.calls
    assert adapter.calls[-3:] == [
        ("send-keys", "-t", "%42", "Tab"),
        ("send-keys", "-t", "%42", "C-m"),
        ("send-keys", "-t", "%42", "C-m"),
    ]


def test_send_skill_invocation_to_pane_does_not_tab_sink_claude(monkeypatch):
    adapter = RecordingAdapter()
    monkeypatch.setattr(skill_invoke.time, "sleep", lambda _: None)

    text = send_skill_invocation_to_pane(adapter, "%42", "preplan", agent="claude")

    assert text == "/preplan "
    assert ("send-keys", "-t", "%42", "Tab") not in adapter.calls


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
        return subprocess.CompletedProcess(cmd, 0, "node /usr/local/bin/" + "codex\n", "")

    monkeypatch.setattr(skill_invoke.subprocess, "run", fake_run)

    assert skill_invoke.resolve_agent_for_pane(adapter, "%42") == "codex"


def test_resolve_agent_for_pane_falls_back_to_pane_hint(monkeypatch):
    adapter = RecordingAdapter()
    adapter.options[("%42", "@PLANNING_AGENT")] = "codex"
    monkeypatch.setattr(
        skill_invoke, "fetch_instance_registry", lambda: (_ for _ in ()).throw(RuntimeError("down"))
    )

    assert skill_invoke.resolve_agent_for_pane(adapter, "%42") == "codex"


def test_resolve_agent_for_pane_prefers_token_api_engine_hint_over_stale_fallbacks(
    monkeypatch,
):
    adapter = RecordingAdapter()
    adapter.options[("%42", "@TOKEN_API_ENGINE")] = "codex"
    adapter.options[("%42", "@PLANNING_AGENT")] = "claude"
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
        # Pane process is inconclusive; @TOKEN_API_ENGINE must beat the stopped
        # registry row and older @PLANNING_AGENT hint.
        return subprocess.CompletedProcess(cmd, 0, "bash -l\n", "")

    monkeypatch.setattr(skill_invoke.subprocess, "run", fake_run)

    assert skill_invoke.resolve_agent_for_pane(adapter, "%42", default="auto") == "codex"


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


def test_send_invocation_to_pane_command_skips_agent_resolution_and_tab(monkeypatch):
    adapter = RecordingAdapter()
    monkeypatch.setattr(skill_invoke.time, "sleep", lambda _: None)

    def explode(*_a, **_k):
        raise AssertionError("commands must not resolve an engine")

    monkeypatch.setattr(skill_invoke, "resolve_agent_for_pane", explode)

    text = send_invocation_to_pane(adapter, "%42", "plan", agent="auto", kind="command")

    assert text == "/plan "
    assert ("send-keys", "-t", "%42", "Tab") not in adapter.calls
    assert adapter.calls[-2:] == [
        ("send-keys", "-t", "%42", "C-m"),
        ("send-keys", "-t", "%42", "C-m"),
    ]
