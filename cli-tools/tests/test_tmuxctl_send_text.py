from __future__ import annotations

import pathlib
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
