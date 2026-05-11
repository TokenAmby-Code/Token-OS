from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import tmuxctl.tmux_adapter as tmux_adapter
from tmuxctl.tmux_adapter import TmuxAdapter


class RecordingAdapter(TmuxAdapter):
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(args)
        return ""


def test_send_text_then_submit_uses_literal_text_and_carriage_return():
    adapter = RecordingAdapter()

    adapter.send_text_then_submit("%42", "hello\nworld", submit_settle_seconds=0)

    assert adapter.calls == [
        ("send-keys", "-t", "%42", "-l", "hello\nworld"),
        ("send-keys", "-t", "%42", "C-m"),
    ]
    assert not any("Enter" in call for call in adapter.calls)


def test_send_text_then_submit_can_clear_prompt_first():
    adapter = RecordingAdapter()

    adapter.send_text_then_submit("%42", "hello", clear_prompt=True, submit_settle_seconds=0)

    assert adapter.calls == [
        ("send-keys", "-t", "%42", "C-u"),
        ("send-keys", "-t", "%42", "-l", "hello"),
        ("send-keys", "-t", "%42", "C-m"),
    ]


def test_send_text_then_submit_waits_before_submit_by_default(monkeypatch):
    adapter = RecordingAdapter()
    sleeps: list[float] = []
    monkeypatch.setattr(tmux_adapter.time, "sleep", sleeps.append)

    adapter.send_text_then_submit("%42", "hello")

    assert sleeps == [tmux_adapter.DEFAULT_SUBMIT_SETTLE_SECONDS]
    assert adapter.calls == [
        ("send-keys", "-t", "%42", "-l", "hello"),
        ("send-keys", "-t", "%42", "C-m"),
        ("send-keys", "-t", "%42", "C-m"),
    ]
