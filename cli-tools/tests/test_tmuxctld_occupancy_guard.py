from __future__ import annotations

import json
import pathlib
import sys
import threading
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import daemon


@pytest.fixture(autouse=True)
def _isolate_wrapper_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUXCTLD_WRAPPER_LEDGER_PATH", str(tmp_path / "wrapper-ledger.json"))
    from tmuxctl import wrapper_ledger

    wrapper_ledger.LEDGER._rows = {}
    wrapper_ledger.LEDGER._loaded = False
    wrapper_ledger.LEDGER.load(force=True)
    yield
    wrapper_ledger.LEDGER._rows = {}
    wrapper_ledger.LEDGER._loaded = False


class SingletonAdapter:
    pane_id = "%3"
    pane_role = "legion:custodes"
    window_name = "legion"

    def __init__(self) -> None:
        self.sends: list[tuple[str, ...]] = []

    def list_sessions(self) -> list:
        return []

    def current_session_name(self) -> str:
        return "main"

    def list_windows(self, session_name: str) -> list[dict[str, str]]:
        return [{"window_index": "1"}]

    def list_panes(self, target: str) -> list[dict[str, str]]:
        return [
            {
                "pane_id": self.pane_id,
                "session_name": "main",
                "window_index": "1",
                "window_name": self.window_name,
                "pane_index": "0",
                "width": "80",
                "height": "24",
                "current_command": "zsh",
                "tty": "/dev/ttys000",
                "active": "1",
            }
        ]

    def show_window_option(self, target: str, option: str) -> str:
        return ""

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[0] == "display-message":
            return f"{self.pane_id}\t{self.pane_role}\t{self.window_name}\t999\t0"
        if args[0] == "send-keys":
            self.sends.append(tuple(args))
            return ""
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.sends.append(("send-keys", "-t", target, *keys))

    def show_pane_option(self, pane_id: str, option: str) -> str:
        if option == "@PANE_ID":
            return self.pane_role
        return ""


class EmptyWorkerAdapter(SingletonAdapter):
    pane_id = "%9"
    pane_role = "mechanicus:1"
    window_name = "mechanicus"

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[0] == "display-message":
            return f"{self.pane_id}\t{self.pane_role}\t{self.window_name}\t1000\t0"
        return super().run(*args, allow_failure=allow_failure)


def _serve(adapter):
    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0), adapter_factory=lambda: adapter, version="t", sha="t"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    assert server.ready.wait(timeout=5)
    return server


def _post(server, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_daemon_send_text_refuses_dispatch_clear_into_singleton(monkeypatch):
    monkeypatch.setattr(daemon.typing_guard_state, "hold", lambda *a, **k: False)
    monkeypatch.setattr(daemon.typing_guard_state, "release", lambda *a, **k: None)
    monkeypatch.setattr(daemon.send_gate, "evaluate", lambda *a, **k: None)
    adapter = SingletonAdapter()
    server = _serve(adapter)
    try:
        payload = _post(server, "/send-text", {"pane": "legion:custodes", "text": "clear"})
    finally:
        server.shutdown()

    assert payload["ok"] is False
    assert "protected singleton" in payload["error"]["message"]
    assert adapter.sends == []


def test_daemon_send_text_allows_dispatch_clear_into_empty_worker(monkeypatch):
    monkeypatch.setattr(daemon.typing_guard_state, "hold", lambda *a, **k: False)
    monkeypatch.setattr(daemon.typing_guard_state, "release", lambda *a, **k: None)
    monkeypatch.setattr(daemon.send_gate, "evaluate", lambda *a, **k: None)
    adapter = EmptyWorkerAdapter()
    server = _serve(adapter)
    try:
        payload = _post(
            server,
            "/send-text",
            {"pane": "mechanicus:1", "text": "clear", "verify": False},
        )
    finally:
        server.shutdown()

    assert payload["ok"] is True
    assert adapter.sends


class LedgerGateAdapter:
    def __init__(
        self, *, role="palace:N", pane="%90", pid=1900, expose_wrapper_stamp: bool = False
    ) -> None:
        self.pane_id = pane
        self.pane_role = role
        self.window_name = role.split(":", 1)[0]
        self.pane_pid = pid
        self.expose_wrapper_stamp = expose_wrapper_stamp
        self.sends: list[tuple[str, ...]] = []
        self.buffer = ""

    def list_sessions(self) -> list:
        return []

    def current_session_name(self) -> str:
        return "main"

    def list_windows(self, session_name: str) -> list[dict[str, str]]:
        return [{"window_index": "1"}]

    def list_panes(self, target: str) -> list[dict[str, str]]:
        return [
            {
                "pane_id": self.pane_id,
                "session_name": "main",
                "window_index": "1",
                "window_name": self.window_name,
                "pane_index": "0",
                "width": "80",
                "height": "24",
                "current_command": "zsh",
                "tty": "/dev/ttys000",
                "active": "1",
            }
        ]

    def show_window_option(self, target: str, option: str) -> str:
        return ""

    def _display_row(self) -> str:
        return f"{self.pane_id}	{self.pane_role}	{self.window_name}	{self.pane_pid}	0"

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:3] == ("list-panes", "-a", "-F") and self.expose_wrapper_stamp:
            from tmuxctl import wrapper_ledger

            sep = wrapper_ledger._SCAN_SEP
            return sep.join(
                [
                    f"wrap-{self.pane_role}",
                    "",
                    f"inst-{self.pane_role}",
                    "worker",
                    self.pane_role,
                    "codex",
                    "/tmp/fake-agent",
                    "123.5",
                    "0",
                ]
            )
        if args[0] == "display-message":
            fmt = args[-1]
            if fmt == "#{pane_pid}":
                return str(self.pane_pid)
            if fmt == "#{pane_id}":
                return self.pane_id
            if fmt == "#{window_name}":
                return self.window_name
            return self._display_row()
        if args[0] == "capture-pane":
            return self.buffer
        if args[0] == "send-keys":
            self.sends.append(tuple(args))
            if "-l" in args:
                self.buffer += str(args[args.index("-l") + 1])
            return ""
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.sends.append(("send-keys-helper", target, *keys))

    def show_pane_option(self, pane_id: str, option: str) -> str:
        if option == "@PANE_ID":
            return self.pane_role
        return ""


def _seed_ledger(role: str = "palace:N") -> None:
    from tmuxctl import wrapper_ledger

    wrapper_ledger.LEDGER.upsert(
        wrapper_id=f"wrap-{role}",
        instance_id=f"inst-{role}",
        pane_positional_id=role,
        engine="codex",
        state="OPEN",
    )


def test_comms_send_proceeds_when_ledger_and_sniff_occupied(monkeypatch):
    from tmuxctl import occupancy

    monkeypatch.setattr(occupancy, "_active_agent", lambda pane_pid: pane_pid == 1900)
    _seed_ledger("palace:N")
    adapter = LedgerGateAdapter(role="palace:N", pid=1900, expose_wrapper_stamp=True)
    server = _serve(adapter)
    try:
        payload = _post(server, "/send-text", {"pane": "%90", "text": "hello", "submit": False})
    finally:
        server.shutdown()

    assert payload["ok"] is True
    assert payload["result"]["status"] == "inserted"
    assert adapter.sends


def test_comms_send_refuses_blank_ledger_unoccupied_pane(monkeypatch):
    from tmuxctl import occupancy

    monkeypatch.setattr(occupancy, "_active_agent", lambda pane_pid: False)
    adapter = LedgerGateAdapter(role="palace:S", pid=1901)
    server = _serve(adapter)
    try:
        payload = _post(server, "/send-text", {"pane": "%90", "text": "hello", "submit": False})
    finally:
        server.shutdown()

    assert payload["ok"] is False
    assert payload["error"]["code"] == "ValueError"
    assert "ledger_unoccupied" in payload["error"]["message"]
    assert adapter.sends == []


def test_comms_send_loud_p0_when_ledger_and_sniff_disagree(monkeypatch):
    from tmuxctl import occupancy, wrapper_ledger

    adapter = LedgerGateAdapter(role="palace:E", pid=1902, expose_wrapper_stamp=True)

    _seed_ledger("palace:E")
    monkeypatch.setattr(occupancy, "_active_agent", lambda pane_pid: False)
    server = _serve(adapter)
    try:
        occupied_empty = _post(
            server, "/send-text", {"pane": "%90", "text": "hello", "submit": False}
        )
    finally:
        server.shutdown()

    wrapper_ledger.LEDGER.close("wrap-palace:E")
    monkeypatch.setattr(occupancy, "_active_agent", lambda pane_pid: pane_pid == 1902)
    adapter2 = LedgerGateAdapter(role="palace:E", pid=1902)
    server = _serve(adapter2)
    try:
        empty_occupied = _post(
            server, "/send-text", {"pane": "%90", "text": "hello", "submit": False}
        )
    finally:
        server.shutdown()

    for payload in (occupied_empty, empty_occupied):
        assert payload["ok"] is False
        assert "P0_LEDGER_SNIFF_INCONGRUENCY" in payload["error"]["message"]
    assert adapter.sends == []
    assert adapter2.sends == []


def test_prealloc_new_order_is_palace_and_somnium_specific():
    from tmuxctl.prealloc import first_free_prealloc_role, ordered_prealloc_roles

    assert ordered_prealloc_roles("palace") == ("palace:N", "palace:S", "palace:E", "palace:W")
    assert ordered_prealloc_roles("somnium") == (
        "somnium:N",
        "somnium:NE",
        "somnium:SE",
        "somnium:S",
        "somnium:W",
    )
    assert (
        first_free_prealloc_role(
            "palace",
            [
                {"pane_role": "palace:W", "window_name": "palace"},
                {"pane_role": "palace:E", "window_name": "palace"},
                {"pane_role": "palace:S", "window_name": "palace"},
            ],
        )
        == "palace:S"
    )
    assert (
        first_free_prealloc_role(
            "somnium",
            [
                {"pane_role": "somnium:W", "window_name": "somnium"},
                {"pane_role": "somnium:S", "window_name": "somnium"},
                {"pane_role": "somnium:SE", "window_name": "somnium"},
            ],
        )
        == "somnium:SE"
    )


def test_prealloc_freelist_excludes_stale_unbound_live_agent_before_selection(monkeypatch):
    from tmuxctl import occupancy, wrapper_ledger
    from tmuxctl.occupancy import assert_dispatch_target_available
    from tmuxctl.resolver import list_free_panes

    class MultiAdapter(LedgerGateAdapter):
        rows = {
            "%N": "%N	palace:N	palace	2001	0",
            "%S": "%S	palace:S	palace	2002	0",
            "%E": "%E	palace:E	palace	2003	0",
        }

        def run(self, *args: str, allow_failure: bool = False) -> str:
            if args[:1] == ("list-panes",):
                return "\n".join(self.rows.values())
            if args[0] == "display-message":
                target = args[args.index("-t") + 1] if "-t" in args else "%N"
                return self.rows[target]
            return super().run(*args, allow_failure=allow_failure)

    wrapper_ledger.LEDGER.upsert(
        wrapper_id="wrap-n", instance_id="inst-n", pane_positional_id="palace:N", engine="codex"
    )
    sniffed: list[int | None] = []

    def fake_active(pane_pid):
        sniffed.append(pane_pid)
        return pane_pid == 2001

    monkeypatch.setattr(occupancy, "_active_agent", fake_active)
    adapter = MultiAdapter()

    free = list_free_panes(adapter)
    assert [p.pane_role for p in free] == ["palace:S", "palace:E"]
    assert sniffed == [2001, 2002, 2003]

    assert_dispatch_target_available(adapter, "%S")
    assert sniffed == [2001, 2002, 2003, 2002]


def test_dispatch_incongruency_reconciles_stale_ledger_before_refusal(monkeypatch):
    from tmuxctl import occupancy

    class Adapter:
        def __init__(self):
            self.calls = 0

        def _resolve_pane_target_arg(self, pane):
            return "%4"

        def run(self, *args, allow_failure=False):
            self.calls += 1
            if args[0] == "display-message":
                return "%4\tmechanicus:4\tmechanicus\t123\t"
            return ""

    stale = {"instance_id": "old"}
    states = [stale, None]
    monkeypatch.setattr(occupancy, "_active_agent", lambda pid: False)
    monkeypatch.setattr(occupancy, "_active_wrapper_row_for_role", lambda role: states.pop(0))

    class Ledger:
        def reconcile_from_tmux(self, adapter):
            return {"open_rows": 0}

    monkeypatch.setitem(
        __import__("sys").modules,
        "tmuxctl.wrapper_ledger",
        type("M", (), {"LEDGER": Ledger()})(),
    )
    occ = occupancy.assert_dispatch_target_available(Adapter(), "mechanicus:4")
    assert occ.pane_role == "mechanicus:4"


def test_comms_incongruency_names_repair_when_live_unbound_persists(monkeypatch):
    from tmuxctl import occupancy

    class Adapter:
        def _resolve_pane_target_arg(self, pane):
            return "%N"

        def run(self, *args, allow_failure=False):
            if args[0] == "display-message":
                return "%N\tsomnium:N\tsomnium\t123\t"
            return ""

    monkeypatch.setattr(occupancy, "_active_agent", lambda pid: True)
    monkeypatch.setattr(occupancy, "_active_wrapper_row_for_role", lambda role: None)
    monkeypatch.setattr(occupancy, "_reconcile_then_reread", lambda adapter, pane: None)

    try:
        occupancy.assert_comms_delivery_target_occupied(Adapter(), "somnium:N")
    except ValueError as exc:
        msg = str(exc)
    else:
        raise AssertionError("expected fail-closed incongruency")
    assert "direction=ledger_empty_agent_live" in msg
    assert "repair_op=tmuxctld_assert_instance_or_restart_live_unbound_pane" in msg
