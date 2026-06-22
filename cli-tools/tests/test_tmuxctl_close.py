from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.close import close_instance, close_pane
from tmuxctl.tmux_adapter import TmuxAdapter


class FakeCloseAdapter:
    def __init__(self, *, role: str = "legion:worker", exists_count: int = 99) -> None:
        self.role = role
        self.exists_count = exists_count
        self.commands: list[tuple[str, ...]] = []
        self.raw_commands: list[tuple[str, ...]] = []
        self.focus_mutation_count = 0

    def show_pane_option(self, pane_id: str, option: str) -> str:
        self.commands.append(("show_pane_option", pane_id, option))
        if option == "@PANE_ID":
            return self.role
        return ""

    def clear_runtime_state(self, target: str) -> None:
        self.commands.append(("clear_runtime_state", target))

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.commands.append(("send-keys", "-t", target, *keys))

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        if args[0] == "display-message" and "-t" in args and args[-1] == "#{pane_id}":
            self.exists_count -= 1
            return "%9\n" if self.exists_count >= 0 else ""
        if args[0] == "display-message" and args[-1] == "#{pane_id}":
            return "%9\n"
        if (
            args[0] == "display-message"
            and args[-1] == "#{session_name}:#{window_index}\t#{window_name}"
        ):
            return "main:3\tlegion\n"
        if args[0] == "display-message" and args[-1] == "#{window_index}":
            return "3\n"
        if args[0] == "display-message" and args[-1] == "#{window_name}":
            return "legion\n"
        if args[0] == "display-message" and args[-1] == "#{window_width}":
            return "120\n"
        if args[0] == "display-message" and args[-1] == "#{window_height}":
            return "40\n"
        if args[0] == "display-message" and args[-1] == "#{window_zoomed_flag}":
            return "0\n"
        if args[0] == "display-message" and args[-1] == "#{session_name}:#{window_name}":
            return "main:legion\n"
        if args[0] == "list-panes":
            return "%C\tlegion:custodes\tlegion\t0\t0\t0\t80\t40\tclaude\tfalse\n"
        return ""


def test_close_pane_refuses_protected_static_persona_panes():
    adapter = FakeCloseAdapter(role="legion:custodes")

    result = close_pane(adapter, "%9")

    assert result["status"] == "refused"
    assert not any(command[0] == "send-keys" for command in adapter.commands)


def test_close_pane_clears_runtime_interrupts_kills_and_enforces_stack():
    adapter = FakeCloseAdapter(exists_count=99)

    result = close_pane(adapter, "%9", timeout=0)

    assert result["status"] == "failed"  # fake still reports pane present after kill
    assert ("clear_runtime_state", "%9") in adapter.commands
    assert adapter.commands.count(("send-keys", "-t", "%9", "C-c")) == 3
    assert ("kill-pane", "-t", "%9") in adapter.commands
    assert result["method"] == "kill-pane"


def test_close_instance_now_delegates_lifecycle_then_pane_close(monkeypatch):
    adapter = FakeCloseAdapter(exists_count=-1)
    calls = []

    def fake_http(method, path, body=None):
        calls.append((method, path, body))
        return {"ok": True}

    monkeypatch.setattr("tmuxctl.close._http_json", fake_http)

    result = close_instance(
        adapter,
        "iid-1",
        lifecycle="retire",
        mode="now",
        pane="%9",
        timeout=0,
    )

    assert calls == [("PATCH", "/api/instances/iid-1/retire", None)]
    assert result["lifecycle_result"] == {"ok": True}
    assert result["close"]["status"] == "already_closed"


def test_close_instance_after_stop_posts_mark_for_close(monkeypatch):
    adapter = FakeCloseAdapter()
    calls = []

    def fake_http(method, path, body=None):
        calls.append((method, path, body))
        return {"success": True}

    monkeypatch.setattr("tmuxctl.close._http_json", fake_http)

    result = close_instance(
        adapter,
        "iid-2",
        lifecycle="banish",
        mode="after-stop",
        pane="%10",
    )

    assert result["status"] == "armed"
    assert calls == [
        (
            "POST",
            "/api/instances/iid-2/mark-for-close",
            {"mode": "after-stop", "lifecycle": "banish", "pane": "%10"},
        )
    ]


class InvariantAdapter(TmuxAdapter):
    def __init__(self) -> None:
        super().__init__(tmux_binary="tmux")
        self.raw: list[tuple[str, ...]] = []

    def _run_raw_tmux(self, args: list[str], *, allow_failure: bool = True) -> str:
        self.raw.append(tuple(args))
        if args[:3] == ["display-message", "-p", "#{pane_id}"]:
            return "%cur\n"
        return ""


def test_unsetting_instance_id_clears_style_first():
    adapter = InvariantAdapter()

    adapter._preflight_runtime_invariants(["set-option", "-pu", "-t", "%9", "@INSTANCE_ID"])

    assert adapter.raw[:2] == [
        ("select-pane", "-t", "%9", "-P", "bg=default"),
        ("select-pane", "-t", "%9", "-T", ""),
    ]


def test_respawn_preflight_clears_runtime_and_style_first():
    adapter = InvariantAdapter()

    adapter._preflight_runtime_invariants(["respawn-pane", "-k", "-t", "%9"])

    assert adapter.raw[0:2] == [
        ("select-pane", "-t", "%9", "-P", "bg=default"),
        ("select-pane", "-t", "%9", "-T", ""),
    ]
    assert ("set-option", "-pu", "-t", "%9", "@INSTANCE_ID") in adapter.raw
