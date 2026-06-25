from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import pytest
import tmuxctl.tmux_adapter as tmux_adapter
from tmuxctl.tmux_adapter import TmuxAdapter, normalize_prompt_payload


class RecordingAdapter(TmuxAdapter):
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(args)
        return ""


def test_normalize_prompt_payload_collapses_newlines_and_trims():
    assert normalize_prompt_payload("hello\n\rworld  \n") == "hello world"


def test_normalize_prompt_payload_rejects_empty():
    with pytest.raises(ValueError):
        normalize_prompt_payload(" \n\r\n")


def test_send_text_then_submit_uses_literal_text_delay_and_double_carriage_return(monkeypatch):
    adapter = RecordingAdapter()
    sleeps: list[float] = []
    monkeypatch.setattr(tmux_adapter.time, "sleep", sleeps.append)

    adapter.send_text_then_submit("%42", "hello\nworld")

    assert sleeps == [1.0, 1.0]
    assert adapter.calls == [
        ("send-keys", "-t", "%42", "-l", "hello world"),
        ("send-keys", "-t", "%42", "C-m"),
        ("send-keys", "-t", "%42", "C-m"),
    ]
    assert not any("Enter" in call for call in adapter.calls)


def test_send_text_then_submit_can_clear_prompt_first(monkeypatch):
    adapter = RecordingAdapter()
    monkeypatch.setattr(tmux_adapter.time, "sleep", lambda _: None)

    adapter.send_text_then_submit("%42", "hello", clear_prompt=True)

    assert adapter.calls == [
        ("send-keys", "-t", "%42", "C-u"),
        ("send-keys", "-t", "%42", "-l", "hello"),
        ("send-keys", "-t", "%42", "C-m"),
        ("send-keys", "-t", "%42", "C-m"),
    ]


def test_automation_focus_guard_blocks_mechanicus_select_pane(monkeypatch):
    adapter = TmuxAdapter(tmux_binary="tmux")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["display-message", "-t"]:
            return subprocess.CompletedProcess(cmd, 0, "mechanicus\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setenv("IMPERIUM_TMUX_AUTOMATION", "1")
    monkeypatch.setattr(tmux_adapter.subprocess, "run", fake_run)

    assert adapter.run("select-pane", "-t", "%42") == ""

    assert ["tmux", "display-message", "-t", "%42", "-p", "#{window_name}"] in calls
    assert ["tmux", "select-pane", "-t", "%42"] not in calls


def test_focus_guard_allow_env_opens_override_and_executes(monkeypatch):
    adapter = TmuxAdapter(tmux_binary="tmux")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["display-message", "-t"]:
            return subprocess.CompletedProcess(cmd, 0, "mechanicus\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setenv("IMPERIUM_ALLOW_MECHANICUS_FOCUS", "1")
    monkeypatch.setattr(tmux_adapter.subprocess, "run", fake_run)

    adapter.run("select-pane", "-t", "%42")

    assert any(
        cmd[:4] == ["tmux", "set-option", "-g", "@IMPERIUM_ALLOW_MECHANICUS_FOCUS_UNTIL"]
        for cmd in calls
    )
    assert ["tmux", "select-pane", "-t", "%42"] in calls


def test_automation_focus_guard_does_not_block_style_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = TmuxAdapter(tmux_binary="tmux")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setenv("IMPERIUM_TMUX_AUTOMATION", "1")
    monkeypatch.setattr(tmux_adapter.subprocess, "run", fake_run)

    adapter.run("set-option", "-p", "-t", "%42", "window-style", "bg=#300808")

    assert calls == [["tmux", "set-option", "-p", "-t", "%42", "window-style", "bg=#300808"]]


def test_set_pane_tint_uses_pane_options_not_select_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = TmuxAdapter(tmux_binary="tmux")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(tmux_adapter.subprocess, "run", fake_run)

    adapter.set_pane_tint("%42", "#300808")

    assert calls == [
        ["tmux", "set-option", "-p", "-t", "%42", "window-style", "bg=#300808"],
        ["tmux", "set-option", "-p", "-t", "%42", "window-active-style", "bg=#300808"],
    ]


def test_clear_pane_tint_unsets_pane_style_options(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = TmuxAdapter(tmux_binary="tmux")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(tmux_adapter.subprocess, "run", fake_run)

    adapter.set_pane_tint("%42", "default")

    assert calls == [
        ["tmux", "set-option", "-pu", "-t", "%42", "window-style"],
        ["tmux", "set-option", "-pu", "-t", "%42", "window-active-style"],
    ]


def test_automation_focus_guard_blocks_non_mechanicus_select_pane(monkeypatch):
    adapter = TmuxAdapter(tmux_binary="tmux")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["display-message", "-t"]:
            return subprocess.CompletedProcess(cmd, 0, "palace\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setenv("IMPERIUM_TMUX_AUTOMATION", "1")
    monkeypatch.setattr(tmux_adapter.subprocess, "run", fake_run)

    assert adapter.run("select-pane", "-t", "%42") == ""

    assert ["tmux", "select-pane", "-t", "%42"] not in calls


def test_focus_restore_env_allows_automation_restore(monkeypatch):
    adapter = TmuxAdapter(tmux_binary="tmux")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["display-message", "-t"]:
            return subprocess.CompletedProcess(cmd, 0, "mechanicus\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setenv("IMPERIUM_TMUX_AUTOMATION", "1")
    monkeypatch.setenv("IMPERIUM_TMUX_FOCUS_RESTORE", "1")
    monkeypatch.setattr(tmux_adapter.subprocess, "run", fake_run)

    adapter.run("select-pane", "-t", "%42")

    assert ["tmux", "select-pane", "-t", "%42"] in calls
