from __future__ import annotations

import pathlib
import signal
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import close as close_mod
from tmuxctl.close import close_contract_signal_shield, close_instance, close_pane
from tmuxctl.liveness import LiveTui
from tmuxctl.tmux_adapter import TmuxAdapter


class FakeCloseAdapter:
    def __init__(
        self,
        *,
        role: str = "mechanicus:worker",
        window_name: str = "council",
        exists_count: int = 99,
        pane_dead: bool = False,
    ) -> None:
        self.role = role
        self.window_name = window_name
        self.exists_count = exists_count
        self.pane_dead = pane_dead
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
            return f"main:3\t{self.window_name}\n"
        if args[0] == "display-message" and args[-1] == "#{window_index}":
            return "3\n"
        if args[0] == "display-message" and args[-1] == "#{window_name}":
            return f"{self.window_name}\n"
        if args[0] == "display-message" and args[-1] == "#{window_width}":
            return "120\n"
        if args[0] == "display-message" and args[-1] == "#{window_height}":
            return "40\n"
        if args[0] == "display-message" and args[-1] == "#{window_zoomed_flag}":
            return "0\n"
        if args[0] == "display-message" and args[-1] == "#{pane_dead}":
            return "1\n" if self.pane_dead else "0\n"
        if args[:2] == ("respawn-pane", "-t"):
            self.pane_dead = False
            return ""
        if args[0] == "display-message" and args[-1] == "#{session_name}:#{window_name}":
            return f"main:{self.window_name}\n"
        if args[0] == "list-panes":
            return "%C\tcouncil:custodes\tcouncil\t0\t0\t0\t80\t40\tclaude\tfalse\n"
        return ""


def test_close_pane_slot_clears_in_place_and_never_kills() -> None:
    adapter = FakeCloseAdapter(role="somnium:S", window_name="somnium", pane_dead=True)

    result = close_pane(adapter, "%9", timeout=0)

    assert result["status"] == "cleared_in_place"
    assert result["pane_class"] == "slot"
    assert result["pane"] == "%9"
    assert ("clear_runtime_state", "%9") in adapter.commands
    assert ("respawn-pane", "-t", "%9") in adapter.commands
    assert not any(command[:1] == ("kill-pane",) for command in adapter.commands)


def test_close_pane_slot_scrubs_before_graceful_wait() -> None:
    adapter = FakeCloseAdapter(role="somnium:N", window_name="somnium", pane_dead=False)

    result = close_pane(adapter, "%9", timeout=0)

    clear_idx = adapter.commands.index(("clear_runtime_state", "%9"))
    first_interrupt_idx = adapter.commands.index(("send-keys", "-t", "%9", "C-c"))
    assert clear_idx < first_interrupt_idx
    assert result["chrome_cleared"] is True
    assert result["pane_freed"] is True


def test_close_pane_worker_reports_partial_when_kill_fails_after_atomic_scrub() -> None:
    adapter = FakeCloseAdapter(role="mechanicus:worker", window_name="mechanicus", exists_count=99)

    result = close_pane(adapter, "%9", timeout=0)

    assert result["status"] == "partial_teardown"
    assert result["reason"] == "kill_pane_failed_after_runtime_clear"
    assert result["chrome_cleared"] is True
    assert result["pane_freed"] is False
    assert result["pane_class"] == "worker"
    assert adapter.commands.count(("send-keys", "-t", "%9", "C-c")) == 3
    assert ("kill-pane", "-t", "%9") in adapter.commands
    assert result["method"] == "kill-pane"


def test_close_pane_perpetual_label_refused_by_class_router() -> None:
    adapter = FakeCloseAdapter(role="council:malcador", window_name="somnium")

    result = close_pane(adapter, "%9", timeout=0)

    assert result["status"] == "refused"
    assert result["reason"] == "perpetual_pane"
    assert result["pane_class"] == "perpetual"
    assert not any(command[0] == "send-keys" for command in adapter.commands)
    assert not any(command[:1] == ("kill-pane",) for command in adapter.commands)


def test_close_instance_now_on_slot_retires_and_preserves_pane(monkeypatch) -> None:
    adapter = FakeCloseAdapter(role="somnium:SE", window_name="somnium", pane_dead=True)
    calls = []

    monkeypatch.setattr(
        "tmuxctl.close._http_json",
        lambda method, path, body=None: calls.append((method, path, body)) or {"ok": True},
    )
    monkeypatch.setattr(close_mod, "instance_live_tui", lambda *a, **k: None)
    monkeypatch.setattr(close_mod, "detect_pane_tui", lambda *a, **k: _dead_tui())

    result = close_instance(adapter, "iid-slot", mode="now", pane="%9", timeout=0)

    assert result["lifecycle_result"] == {"ok": True}
    assert calls == [("PATCH", "/api/instances/iid-slot/retire", None)]
    assert result["close"]["status"] == "cleared_in_place"
    assert result["close"]["pane_class"] == "slot"
    assert ("respawn-pane", "-t", "%9") in adapter.commands
    assert not any(command[:1] == ("kill-pane",) for command in adapter.commands)


def test_close_pane_refuses_protected_static_persona_panes():
    adapter = FakeCloseAdapter(role="council:custodes")

    result = close_pane(adapter, "%9")

    assert result["status"] == "refused"
    assert not any(command[0] == "send-keys" for command in adapter.commands)


def test_close_pane_clears_runtime_interrupts_kills_and_enforces_stack():
    adapter = FakeCloseAdapter(exists_count=99)

    result = close_pane(adapter, "%9", timeout=0)

    assert result["status"] == "partial_teardown"
    assert result["reason"] == "kill_pane_failed_after_runtime_clear"
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


def _live_tui(pane_id: str = "%9") -> LiveTui:
    return LiveTui(pane_id=pane_id, pane_pid=14030, agent_pid=15230, agent_command="claude")


def _dead_tui(pane_id: str = "%9") -> LiveTui:
    return LiveTui(pane_id=pane_id, pane_pid=None, agent_pid=None, agent_command=None)


def test_close_instance_refuses_retire_while_tui_live(monkeypatch):
    """Guard: a live TUI on the target pane must fail closed — no retire, no kill."""
    adapter = FakeCloseAdapter(exists_count=99)
    calls = []

    monkeypatch.setattr(
        "tmuxctl.close._http_json",
        lambda method, path, body=None: calls.append((method, path, body)) or {"ok": True},
    )
    monkeypatch.setattr(close_mod, "instance_live_tui", lambda *a, **k: _live_tui())

    result = close_instance(adapter, "iid-1", mode="now", pane="%9", timeout=0)

    assert result["status"] == "refused"
    assert result["reason"] == "live_tui"
    assert result["agent_pid"] == 15230
    # Fail closed: the DB row was never retired and the pane was never killed.
    assert calls == []
    assert not any(c[0] == "kill-pane" for c in adapter.commands)
    assert not any(c[:1] == ("send-keys",) for c in adapter.commands)


def test_close_instance_force_kills_then_retires_live_tui(monkeypatch):
    """--force: kill the live TUI atomically BEFORE retiring the DB row."""
    adapter = FakeCloseAdapter(exists_count=1)  # present on entry, gone after kill
    events = []

    def fake_http(method, path, body=None):
        events.append(("http", method, path))
        return {"ok": True}

    monkeypatch.setattr("tmuxctl.close._http_json", fake_http)
    monkeypatch.setattr(close_mod, "instance_live_tui", lambda *a, **k: _live_tui())
    monkeypatch.setattr(close_mod, "detect_pane_tui", lambda *a, **k: _dead_tui())

    result = close_instance(adapter, "iid-1", mode="now", pane="%9", timeout=0, force=True)

    assert result["status"] == "closed"
    assert ("kill-pane", "-t", "%9") in adapter.commands
    assert ("http", "PATCH", "/api/instances/iid-1/retire") in events
    # Atomic: the proc kill must precede the DB retire.
    kill_idx = adapter.commands.index(("kill-pane", "-t", "%9"))
    assert kill_idx >= 0
    assert events == [("http", "PATCH", "/api/instances/iid-1/retire")]


def test_close_instance_force_refuses_when_tui_survives_close(monkeypatch):
    """If a forced kill fails to clear the TUI, refuse — never retire a live row."""
    adapter = FakeCloseAdapter(exists_count=99)  # pane never dies
    calls = []

    monkeypatch.setattr(
        "tmuxctl.close._http_json",
        lambda method, path, body=None: calls.append((method, path, body)) or {"ok": True},
    )
    monkeypatch.setattr(close_mod, "instance_live_tui", lambda *a, **k: _live_tui())
    monkeypatch.setattr(close_mod, "detect_pane_tui", lambda *a, **k: _live_tui())

    result = close_instance(adapter, "iid-1", mode="now", pane="%9", timeout=0, force=True)

    assert result["status"] == "refused"
    assert result["reason"] == "live_tui_survived_close"
    assert calls == []  # retire never fired


def test_close_instance_idle_husk_reaps_clean(monkeypatch):
    """No live TUI (idle/exited husk): kill the pane, THEN retire — clean reap."""
    adapter = FakeCloseAdapter(exists_count=1)  # present then gone
    calls = []

    monkeypatch.setattr(
        "tmuxctl.close._http_json",
        lambda method, path, body=None: calls.append((method, path, body)) or {"ok": True},
    )
    monkeypatch.setattr(close_mod, "instance_live_tui", lambda *a, **k: None)
    monkeypatch.setattr(close_mod, "detect_pane_tui", lambda *a, **k: _dead_tui())

    result = close_instance(adapter, "iid-1", mode="now", pane="%9", timeout=0)

    assert result["status"] == "closed"
    assert calls == [("PATCH", "/api/instances/iid-1/retire", None)]
    assert ("kill-pane", "-t", "%9") in adapter.commands


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


def test_unsetting_instance_id_clears_style_first() -> None:
    adapter = InvariantAdapter()

    adapter._preflight_runtime_invariants(["set-option", "-pu", "-t", "%9", "@INSTANCE_ID"])

    assert adapter.raw[1:3] == [
        ("set-option", "-pu", "-t", "%9", "window-style"),
        ("set-option", "-pu", "-t", "%9", "window-active-style"),
    ]
    assert ("select-pane", "-t", "%9", "-T", "") in adapter.raw


def test_respawn_preflight_clears_runtime_and_style_first() -> None:
    adapter = InvariantAdapter()

    adapter._preflight_runtime_invariants(["respawn-pane", "-k", "-t", "%9"])

    assert adapter.raw[0:2] == [
        ("set-option", "-pu", "-t", "%9", "window-style"),
        ("set-option", "-pu", "-t", "%9", "window-active-style"),
    ]
    assert ("select-pane", "-t", "%9", "-T", "") in adapter.raw
    assert ("set-option", "-pu", "-t", "%9", "@INSTANCE_ID") in adapter.raw


def test_close_contract_signal_shield_ignores_ctrl_c_signals(monkeypatch):
    calls = []

    def fake_signal(sig, handler):
        calls.append((sig, handler))
        return f"old-{sig}"

    monkeypatch.setattr(signal, "signal", fake_signal)

    with close_contract_signal_shield():
        pass

    ignored = calls[:3]
    restored = calls[3:]
    assert ignored == [
        (signal.SIGINT, signal.SIG_IGN),
        (signal.SIGQUIT, signal.SIG_IGN),
        (signal.SIGTSTP, signal.SIG_IGN),
    ]
    assert restored == [
        (signal.SIGINT, f"old-{signal.SIGINT}"),
        (signal.SIGQUIT, f"old-{signal.SIGQUIT}"),
        (signal.SIGTSTP, f"old-{signal.SIGTSTP}"),
    ]
