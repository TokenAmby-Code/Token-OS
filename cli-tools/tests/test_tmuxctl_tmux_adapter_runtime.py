from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import tmuxctl.tmux_adapter as tmux_adapter
from tmuxctl.tmux_adapter import PANE_STYLE_OPTIONS, RUNTIME_PANE_OPTIONS, TmuxAdapter


class _Completed:
    def __init__(self, stdout: str = "") -> None:
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


class _FakeTmux:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        def _run(cmd, *_args, **_kwargs):
            argv = tuple(cmd[1:])
            fake.calls.append(argv)
            if argv[:2] == ("display-message", "-p") and argv[-1] == "#{pane_id}":
                return _Completed("%current\n")
            if argv[0] == "display-message" and argv[-1] == "#{window_name}":
                target = argv[argv.index("-t") + 1] if "-t" in argv else ""
                return _Completed("mechanicus\n" if target.endswith("mechanicus") else "palace\n")
            if argv[0] == "display-message" and argv[-1] == "#{client_tty}":
                return _Completed("/dev/ttys001\n")
            if argv[0] == "display-message" and argv[-1] == "#{session_name}:#{window_index}":
                return _Completed("main:1\n")
            return _Completed("")

        monkeypatch.setattr(tmux_adapter.subprocess, "run", _run)


def test_respawn_pane_clears_runtime_options_and_style_before_respawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux()
    fake.patch(monkeypatch)
    adapter = TmuxAdapter(tmux_binary="tmux")

    adapter.run("respawn-pane", "-k", "-t", "%9", allow_failure=True)

    assert fake.calls[-1] == ("respawn-pane", "-k", "-t", "%9")
    before_respawn = fake.calls[:-1]
    for option in PANE_STYLE_OPTIONS:
        assert ("set-option", "-pu", "-t", "%9", option) in before_respawn
    assert ("select-pane", "-t", "%9", "-T", "") in before_respawn
    for option in RUNTIME_PANE_OPTIONS:
        assert ("set-option", "-pu", "-t", "%9", option) in before_respawn


def test_unsetting_instance_id_clears_pane_style_before_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux()
    fake.patch(monkeypatch)
    adapter = TmuxAdapter(tmux_binary="tmux")

    adapter.run("set-option", "-pu", "-t", "%9", "@INSTANCE_ID", allow_failure=True)

    assert fake.calls[-1] == ("set-option", "-pu", "-t", "%9", "@INSTANCE_ID")
    before_unset = fake.calls[:-1]
    for option in PANE_STYLE_OPTIONS:
        assert ("set-option", "-pu", "-t", "%9", option) in before_unset
    assert ("select-pane", "-t", "%9", "-T", "") in before_unset
    assert ("set-option", "-pu", "-t", "%9", "@PANE_LABEL") not in before_unset


def test_focus_guard_blocks_foreground_mechanicus_creation_but_allows_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux()
    fake.patch(monkeypatch)
    adapter = TmuxAdapter(tmux_binary="tmux")

    assert adapter.run("split-window", "-t", "main:mechanicus", allow_failure=True) == ""
    assert ("split-window", "-t", "main:mechanicus") not in fake.calls

    adapter.run("split-window", "-d", "-t", "main:mechanicus", allow_failure=True)
    assert ("split-window", "-d", "-t", "main:mechanicus") in fake.calls

    adapter.run("new-window", "-d", "-t", "main:mechanicus", allow_failure=True)
    assert ("new-window", "-d", "-t", "main:mechanicus") in fake.calls

    before = list(fake.calls)
    assert adapter.run("new-window", "-t", "main:mechanicus", allow_failure=True) == ""
    assert ("new-window", "-t", "main:mechanicus") not in fake.calls[len(before) :]


def test_focus_guard_allows_title_only_select_and_restore_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTmux()
    fake.patch(monkeypatch)
    adapter = TmuxAdapter(tmux_binary="tmux")

    adapter.run("select-pane", "-t", "main:mechanicus", "-T", "worker", allow_failure=True)
    assert ("select-pane", "-t", "main:mechanicus", "-T", "worker") in fake.calls

    before = list(fake.calls)
    assert (
        adapter.run(
            "select-pane", "-Z", "-t", "main:mechanicus", "-T", "worker", allow_failure=True
        )
        == ""
    )
    assert not any(
        call == ("select-pane", "-Z", "-t", "main:mechanicus", "-T", "worker")
        for call in fake.calls[len(before) :]
    )

    monkeypatch.setenv("IMPERIUM_TMUX_FOCUS_RESTORE", "1")
    adapter.run("select-pane", "-Z", "-t", "main:mechanicus", "-T", "worker", allow_failure=True)
    assert ("select-pane", "-Z", "-t", "main:mechanicus", "-T", "worker") in fake.calls
