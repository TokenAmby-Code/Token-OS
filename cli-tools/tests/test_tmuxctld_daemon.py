"""tmuxctld smoke tests: start the real ThreadingHTTPServer in-process against a
stub adapter and hit it over loopback (the in-process pattern from
``test_instance_name_cli.py``). Asserts the ``/health`` shape, the envelope, a
representative endpoint, and 404 on an unknown route."""

from __future__ import annotations

import json
import pathlib
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import daemon, wrapper_ledger
from tmuxctl import service as tmux_service

# Captured before any monkeypatching so the dedicated re-assertion tests can
# restore the genuine method the autouse guard stubs out.
_REAL_MAYBE_REASSERT = daemon.TmuxctldServer.maybe_reassert_lifecycle_hooks


@pytest.fixture(autouse=True)
def _no_live_tmux_guard(monkeypatch, tmp_path):
    """No daemon test may touch a live tmux server (hook-tests-no-live-tmux).

    ``_h_send_text`` now acquires/releases the typing-guard AGENT hold, which
    shells real tmux. Stub it module-wide so the default is "hold DENIED"
    (held=False) — the no-live-tmux outcome — keeping every existing send-path
    assertion unchanged. Tests exercising the hold explicitly re-patch these.
    """
    monkeypatch.setattr(daemon.typing_guard_state, "hold", lambda *a, **k: False)
    monkeypatch.setattr(daemon.typing_guard_state, "release", lambda *a, **k: None)
    monkeypatch.setattr(daemon.send_gate, "evaluate", lambda *a, **k: None)
    monkeypatch.setenv("TMUXCTLD_WRAPPER_LEDGER_PATH", str(tmp_path / "wrapper-ledger.json"))
    wrapper_ledger.LEDGER._rows = {}
    wrapper_ledger.LEDGER._loaded = False
    wrapper_ledger.LEDGER.load(force=True)
    # /health now re-asserts the tmux lifecycle hooks (which shells real tmux).
    # Neutralise the heartbeat-driven re-assertion module-wide so no /health test
    # touches live tmux; the dedicated re-assertion tests restore the real method.
    # (ensure_tmux_lifecycle_hooks itself is left intact for the startup tests.)
    monkeypatch.setattr(daemon.TmuxctldServer, "maybe_reassert_lifecycle_hooks", lambda self: False)


class StubAdapter:
    """Minimal adapter: tmux reachable, every scan returns empty (fail-closed)."""

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.run("show-options", "-pv", "-t", pane_id, option, allow_failure=True).strip()


class RecordingVoiceAdapter(StubAdapter):
    def __init__(self) -> None:
        self.calls = []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(args)
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.calls.append(("send-keys-helper", target, *keys))


def _serve(adapter_factory):
    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0),
        adapter_factory=adapter_factory,
        version="9.9.9",
        sha="deadbee",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Gate on the real ready event (set once the accept loop owns the socket) —
    # no sleep-based race. The timeout is only a deadlock backstop, not the gate.
    assert server.ready.wait(timeout=5), "server thread never signalled ready"
    return server, thread


def _get(server, path: str):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(server, path: str, body):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post_timeout(server, path: str, body, *, timeout: float = 5):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_server_signals_ready_event() -> None:
    # Finding #3: the server thread sets a threading.Event once it is actually
    # listening; _serve gates on it, so by the time it returns the event is set.
    server, _ = _serve(StubAdapter)
    try:
        assert server.ready.is_set()
    finally:
        server.shutdown()


def test_health_shape() -> None:
    server, _ = _serve(StubAdapter)
    try:
        status, payload = _get(server, "/health")
        assert status == 200
        # /health is the one un-enveloped surface.
        assert payload["ok"] is True
        assert payload["tmux_reachable"] is True
        assert payload["version"] == "9.9.9"
        assert payload["sha"] == "deadbee"
        assert "port" in payload
    finally:
        server.shutdown()


def test_typing_guard_state_endpoint_routes_supported_json_commands(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_main(argv):
        calls.append(list(argv))
        cmd = argv[0]
        print(
            json.dumps(
                {
                    "kind": "agent" if cmd == "hold" else cmd,
                    "active": cmd != "release",
                    "owner": "req-1" if cmd == "hold" else None,
                }
            )
        )
        return 0

    monkeypatch.setattr(daemon.typing_guard_state, "main", fake_main)
    server, _ = _serve(StubAdapter)
    try:
        for cmd in ("arm", "pending", "hold", "release", "expire-pane", "status"):
            status, payload = _post(
                server,
                "/typing-guard-state",
                {"cmd": cmd, "pane": "%42", "seconds": 8, "owner": "req-1"},
            )
            assert status == 200
            assert payload["ok"] is True
            assert payload["result"]["returncode"] == 0
        assert [call[0] for call in calls] == [
            "arm",
            "pending",
            "hold",
            "release",
            "expire-pane",
            "status",
        ]
        assert all("--pane" in call and "%42" in call for call in calls)
    finally:
        server.shutdown()


def test_typing_guard_state_endpoint_rejects_unknown_command() -> None:
    server, _ = _serve(StubAdapter)
    try:
        status, payload = _post(server, "/typing-guard-state", {"cmd": "legacy", "pane": "%42"})
        assert status == 200
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ValueError"
    finally:
        server.shutdown()


def test_typing_guard_arm_disables_any_and_pending_reenables_any() -> None:
    state: dict[str, str] = {}
    calls: list[tuple[str, ...]] = []

    class FakeTmux:
        def run(self, *args: str, timeout: float = 0.5):  # noqa: ARG002
            calls.append(tuple(args))

            class Proc:
                returncode = 0
                stdout = ""

            proc = Proc()
            if args and args[0] == "show-options":
                proc.stdout = state.get(args[-1], "")
            elif args[:2] == ("set-option", "-p"):
                state[args[-2]] = args[-1]
            return proc

    fake = FakeTmux()
    daemon.typing_guard_state.arm(fake, "%42", seconds=300, now=100)
    assert json.loads(state[daemon.typing_guard_state.GUARD_JSON_OPTION])["kind"] == "human"
    assert ("unbind-key", "-q", "-n", "Any") in calls

    calls.clear()
    daemon.typing_guard_state.pending(fake, "%42", seconds=15, now=110)
    assert json.loads(state[daemon.typing_guard_state.GUARD_JSON_OPTION])["kind"] == "pending"
    assert any(call[0] == "source-file" for call in calls), "pending must re-enable root Any"
    assert not any(call[:4] == ("unbind-key", "-q", "-n", "Any") for call in calls)


def test_typing_guard_rehydrate_reads_projections_without_mutating_state() -> None:
    calls: list[tuple[str, ...]] = []
    projections = {
        daemon.typing_guard_state.GUARD_KIND_OPTION: "human",
        daemon.typing_guard_state.GUARD_UNTIL_OPTION: "999",
    }

    class FakeTmux:
        def run(self, *args: str, timeout: float = 0.5):  # noqa: ARG002
            calls.append(tuple(args))

            class Proc:
                returncode = 0
                stdout = ""

            proc = Proc()
            if args and args[0] == "show-options":
                proc.stdout = projections.get(args[-1], "")
            return proc

    result = daemon.typing_guard_state.rehydrate_any_binding(FakeTmux(), "%42", now=100)
    assert result["topology"] == "disabled"
    assert ("unbind-key", "-q", "-n", "Any") in calls
    assert not any(call and call[0] == "set-option" for call in calls)
    assert not any(daemon.typing_guard_state.GUARD_JSON_OPTION in call for call in calls)

    calls.clear()
    projections[daemon.typing_guard_state.GUARD_KIND_OPTION] = "pending"
    result = daemon.typing_guard_state.rehydrate_any_binding(FakeTmux(), "%42", now=100)
    assert result["topology"] == "enabled"
    assert any(call and call[0] == "source-file" for call in calls)
    assert not any(call and call[0] == "set-option" for call in calls)


def test_typing_guard_topology_endpoint_rehydrates_without_state_command(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_rehydrate(tmux, pane="", *, now=None):  # noqa: ARG001
        calls.append((pane, str(now)))
        return {"topology": "enabled", "pane": pane}

    monkeypatch.setattr(daemon.typing_guard_state, "rehydrate_any_binding", fake_rehydrate)
    server, _ = _serve(StubAdapter)
    try:
        status, payload = _post(
            server,
            "/typing-guard-topology",
            {"cmd": "rehydrate", "pane": "%42", "now": "123"},
        )
        assert status == 200
        assert payload["ok"] is True
        assert payload["result"]["topology"] == "enabled"
        assert calls == [("%42", "123")]
    finally:
        server.shutdown()


def test_typing_guard_arm_schedules_expiry_rehydrate(monkeypatch) -> None:
    fired = threading.Event()

    def fake_rehydrate(*args, **kwargs):  # noqa: ANN002, ANN003
        fired.set()
        return {"topology": "enabled"}

    monkeypatch.setattr(daemon.typing_guard_state, "rehydrate_any_binding", fake_rehydrate)
    daemon._schedule_typing_guard_expiry_rehydrate(
        {"kind": "human", "active": True, "until": time.time() - 1}
    )
    assert fired.wait(timeout=1)


def test_resolve_instance_fail_closed_envelope() -> None:
    server, _ = _serve(StubAdapter)
    try:
        status, payload = _get(server, "/tmux/resolve-instance?instance_id=does-not-exist")
        assert status == 200
        assert payload["ok"] is True
        result = payload["result"]
        # Canonical-only, fail-closed: no live pane -> found:false, no id, no 500.
        assert result["instance_id"] == "does-not-exist"
        assert result["found"] is False
        assert result["pane_id"] == ""
        assert result["pane_role"] == ""
    finally:
        server.shutdown()


class FoundInstanceAdapter:
    """tmux reachable; one live pane carries @INSTANCE_ID=my-uuid @PANE_ID=mechanicus:1."""

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:2] == ("list-panes", "-a"):
            return "%42\tmy-uuid\tmechanicus:1"
        return ""


def test_resolve_instance_returns_canonical_role_never_physical() -> None:
    server, _ = _serve(FoundInstanceAdapter)
    try:
        _, payload = _get(server, "/tmux/resolve-instance?instance_id=my-uuid")
        result = payload["result"]
        assert result["found"] is True
        # The canonical {page}:{id} role — NOT the raw physical %42.
        assert result["pane_id"] == "mechanicus:1"
        assert result["pane_role"] == "mechanicus:1"
        assert "%42" not in json.dumps(payload)
    finally:
        server.shutdown()


class StampedPaneAdapter:
    """current pane resolves to %7 and carries @INSTANCE_ID=stamped-uuid."""

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:2] == ("display-message", "-p"):
            return "%7"
        if args[0] == "show-options" and args[-1] == "@INSTANCE_ID":
            return "stamped-uuid"
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.run("show-options", "-pv", "-t", pane_id, option, allow_failure=True).strip()


def test_instance_id_for_pane_reads_stamp() -> None:
    server, _ = _serve(StampedPaneAdapter)
    try:
        status, payload = _get(server, "/tmux/instance-id-for-pane?pane=current")
        assert status == 200
        result = payload["result"]
        assert result["found"] is True
        assert result["instance_id"] == "stamped-uuid"
        assert result["pane"] == "%7"
    finally:
        server.shutdown()


def test_instance_id_for_pane_fail_closed_when_unstamped() -> None:
    server, _ = _serve(StubAdapter)
    try:
        status, payload = _get(server, "/tmux/instance-id-for-pane?pane=current")
        assert status == 200
        result = payload["result"]
        # No @INSTANCE_ID stamp -> fail-closed: found:false, empty instance_id.
        assert result["found"] is False
        assert result["instance_id"] == ""
    finally:
        server.shutdown()


class WrapperEndAdapter:
    def __init__(self) -> None:
        self.wrapper_owner = "wrap-1"
        self.instance_id = "inst-1"
        self.pane_dead = "0"
        self.pane_role = "mechanicus:1"
        self.cleared: list[str] = []
        self.calls: list[tuple[str, ...]] = []

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(tuple(args))
        if args[:3] == ("display-message", "-t", "%42"):
            if args[-1] == "#{pane_id}":
                return "%42" if self.instance_id or self.wrapper_owner else ""
            if args[-1] == "#{pane_dead}":
                return self.pane_dead
        if args[:3] == ("list-panes", "-a", "-F"):
            return f"%42__TMUXCTLD_WRAPPEREND_FIELD__{self.wrapper_owner}"
        if args[:5] == ("set-option", "-p", "-t", "%42", "@TOKEN_API_WRAPPER_LAUNCH_ID"):
            self.wrapper_owner = args[-1]
            return ""
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        if option == "@TOKEN_API_WRAPPER_LAUNCH_ID":
            return self.wrapper_owner
        if option == "@INSTANCE_ID":
            return self.instance_id
        if option == "@PANE_ID":
            return self.pane_role
        return ""

    def clear_runtime_state(self, target: str) -> None:
        self.cleared.append(target)
        self.wrapper_owner = ""
        self.instance_id = ""


def test_wrapperend_clears_owned_runtime_state_immediately_and_idempotently() -> None:
    rec = WrapperEndAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/hooks/wrapperend",
            {"wrapper_launch_id": "wrap-1", "tmux_pane": "%42", "exit_code": 0},
        )
        assert payload["ok"] is True
        assert payload["result"]["status"] == "cleared"
        assert rec.cleared == ["%42"]

        _, duplicate = _post(
            server,
            "/hooks/wrapperend",
            {"wrapper_launch_id": "wrap-1", "tmux_pane": "%42", "exit_code": 0},
        )
        assert duplicate["ok"] is True
        assert duplicate["result"]["status"] in {"already_cleared", "already_missing"}
        assert rec.cleared == ["%42"]
    finally:
        server.shutdown()


def test_wrapperend_resolves_pane_by_wrapper_id_when_payload_pane_missing() -> None:
    rec = WrapperEndAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(server, "/hooks/wrapperend", {"wrapper_launch_id": "wrap-1"})
        assert payload["ok"] is True
        assert payload["result"]["status"] == "cleared"
        assert rec.cleared == ["%42"]
    finally:
        server.shutdown()


def test_wrapperend_rejects_unowned_mismatched_pane_without_clearing() -> None:
    rec = WrapperEndAdapter()
    rec.wrapper_owner = "other-wrap"
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/hooks/wrapperend",
            {"wrapper_launch_id": "wrap-1", "tmux_pane": "%42"},
        )
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ValueError"
        assert "owned by another wrapper" in payload["error"]["message"]
        assert rec.cleared == []
    finally:
        server.shutdown()


class ComprehensiveWrapperEndAdapter(WrapperEndAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.pane_dead = "1"
        self.options = {
            "@TOKEN_API_WRAPPER_LAUNCH_ID": "wrap-1",
            "@INSTANCE_ID": "inst-1",
            "@PERSONA": "Custodes",
            "@SESSION_DOC": "Stale Doc",
            "@CWD": "old-worktree",
            "@PANE_LABEL": "stale-slug",
            "@PANE_PROGRESS": "50%",
            "@PANE_CLEAN": "1",
            "@PANE_BORN": "123",
            "@CC_STATE": "idle",
            "@TYPING_GUARD_JSON": '{"kind":"agent","owner":"wrap-1","source":"tmuxctld","until":9999999999}',
            "@TYPING_GUARD_UNTIL": "9999999999",
            "@TYPING_GUARD_KIND": "agent",
            "@TYPING_GUARD_MARKER": "#[fg=green]⌨",
            "@GT_FIRE": "123",
            "@DISCORD_VOICE_LOCK": "1",
            "@TOKEN_API_CWD": "/old",
        }
        self.unset_options: list[str] = []
        self.exists_after_kill = False

    def show_pane_option(self, pane_id: str, option: str) -> str:
        if option == "@PANE_ID":
            return self.pane_role
        return self.options.get(option, "")

    def clear_runtime_state(self, target: str) -> None:
        self.cleared.append(target)
        for option in list(self.options):
            self.unset_options.append(option)
            self.options.pop(option, None)

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:3] == ("display-message", "-t", "%42") and args[-1] == "#{pane_id}":
            return (
                "%42" if (self.instance_id or self.wrapper_owner or self.exists_after_kill) else ""
            )
        result = super().run(*args, allow_failure=allow_failure)
        if args[:2] == ("kill-pane", "-t"):
            self.instance_id = ""
            self.wrapper_owner = "" if not self.exists_after_kill else self.wrapper_owner
        if args[:5] == ("set-option", "-p", "-t", "%42", "@TOKEN_API_WRAPPER_LAUNCH_ID"):
            self.options["@TOKEN_API_WRAPPER_LAUNCH_ID"] = args[-1]
        return result


def test_wrapperend_comprehensively_scrubs_identity_status_guard_and_reaps_dead_husk() -> None:
    rec = ComprehensiveWrapperEndAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/hooks/wrapperend",
            {"wrapper_launch_id": "wrap-1", "tmux_pane": "%42", "exit_code": 0},
        )
        assert payload["ok"] is True
        result = payload["result"]
        assert result["status"] == "cleared"
        assert result["reap"]["status"] == "killed"

        for option in (
            "@PERSONA",
            "@INSTANCE_ID",
            "@PANE_LABEL",
            "@PANE_PROGRESS",
            "@PANE_CLEAN",
            "@PANE_BORN",
            "@CC_STATE",
            "@TYPING_GUARD_JSON",
            "@TYPING_GUARD_UNTIL",
            "@TYPING_GUARD_KIND",
            "@TYPING_GUARD_MARKER",
            "@SESSION_DOC",
            "@CWD",
            "@GT_FIRE",
            "@DISCORD_VOICE_LOCK",
            "@TOKEN_API_WRAPPER_LAUNCH_ID",
            "@TOKEN_API_CWD",
        ):
            assert option in rec.unset_options
            assert rec.options.get(option, "") == ""
        assert ("kill-pane", "-t", "%42") in rec.calls
    finally:
        server.shutdown()


class PalaceSlotWrapperEndAdapter(ComprehensiveWrapperEndAdapter):
    """A dead palace SLOT husk: same scrub surface, but in a pre-alloced window."""

    def __init__(self) -> None:
        super().__init__()
        self.pane_role = "palace:N"
        self.window_name = "palace"

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args and args[-1] == "#{window_name}":
            self.calls.append(tuple(args))
            return self.window_name
        return super().run(*args, allow_failure=allow_failure)


def test_wrapperend_clears_palace_slot_in_place_and_never_culls_it() -> None:
    # The morning regression: a completed palace:N worker exited and WrapperEnd
    # CULLED the pre-alloced slot (a later close-pane returned "pane target not
    # found"). The class-gated router must CLEAR IN PLACE instead — slot preserved.
    rec = PalaceSlotWrapperEndAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/hooks/wrapperend",
            {"wrapper_launch_id": "wrap-1", "tmux_pane": "%42", "exit_code": 0},
        )
        assert payload["ok"] is True
        result = payload["result"]
        assert result["status"] == "cleared"
        assert result["teardown"]["pane_class"] == "slot"
        assert result["teardown"]["action"] == "cleared_in_place"
        # The pre-allocated slot is PRESERVED — it must never be killed.
        assert ("kill-pane", "-t", "%42") not in rec.calls
        # Runtime stamps scrubbed (the #483 clear-in-place primitive) ...
        assert "@INSTANCE_ID" in rec.unset_options
        assert rec.options.get("@INSTANCE_ID", "") == ""
        # ... and the dead husk's shell revived in place so the slot returns free.
        assert any(c[:1] == ("respawn-pane",) for c in rec.calls)
    finally:
        server.shutdown()


def test_health_reasserts_lifecycle_hooks_throttled(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon.TmuxctldServer, "maybe_reassert_lifecycle_hooks", _REAL_MAYBE_REASSERT
    )
    server, _ = _serve(StubAdapter)
    try:
        calls = []
        monkeypatch.setattr(
            daemon, "ensure_tmux_lifecycle_hooks", lambda: calls.append(1) or {"ok": True}
        )
        # Boot deadline is 0.0 -> first call re-asserts; an immediate second is throttled.
        assert server.maybe_reassert_lifecycle_hooks() is True
        assert server.maybe_reassert_lifecycle_hooks() is False
        assert len(calls) == 1
        # After the throttle window passes the hook is re-installed again, so a live
        # tmux reload / hook-clear self-heals within one interval.
        server._hook_reassert_deadline = 0.0
        assert server.maybe_reassert_lifecycle_hooks() is True
        assert len(calls) == 2
    finally:
        server.shutdown()


def test_health_endpoint_rides_heartbeat_to_reassert_hooks(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon.TmuxctldServer, "maybe_reassert_lifecycle_hooks", _REAL_MAYBE_REASSERT
    )
    server, _ = _serve(StubAdapter)
    try:
        calls = []
        monkeypatch.setattr(
            daemon, "ensure_tmux_lifecycle_hooks", lambda: calls.append(1) or {"ok": True}
        )
        status, payload = _get(server, "/health")
        assert status == 200
        assert payload["ok"] is True
        assert calls, "/health did not re-assert the lifecycle hooks"
    finally:
        server.shutdown()


def test_wrapperstart_scrubs_persistent_slot_before_reuse_then_registers_new_wrapper() -> None:
    rec = ComprehensiveWrapperEndAdapter()
    rec.pane_dead = "0"
    rec.pane_role = "palace:N"
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/hooks/wrapperstart",
            {"wrapper_launch_id": "wrap-new", "tmux_pane": "%42"},
        )
        assert payload["ok"] is True
        assert payload["result"]["status"] == "stamped"
        assert rec.options.get("@PERSONA", "") == ""
        assert rec.options.get("@INSTANCE_ID", "") == ""
        assert rec.options.get("@PANE_LABEL", "") == ""
        assert rec.options.get("@TOKEN_API_WRAPPER_LAUNCH_ID") == "wrap-new"
    finally:
        server.shutdown()


def test_wrapperstart_duplicate_same_wrapper_does_not_scrub_own_runtime() -> None:
    rec = ComprehensiveWrapperEndAdapter()
    rec.pane_dead = "0"
    rec.options["@TOKEN_API_WRAPPER_LAUNCH_ID"] = "wrap-1"
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/hooks/wrapperstart",
            {"wrapper_launch_id": "wrap-1", "tmux_pane": "%42"},
        )
        assert payload["ok"] is True
        assert payload["result"]["status"] == "stamped"
        assert rec.cleared == []
        assert rec.options.get("@PERSONA") == "Custodes"
    finally:
        server.shutdown()


def test_wrapperend_reports_failed_reap_when_dead_husk_survives_kill() -> None:
    rec = ComprehensiveWrapperEndAdapter()
    rec.exists_after_kill = True
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/hooks/wrapperend",
            {"wrapper_launch_id": "wrap-1", "tmux_pane": "%42", "exit_code": 0},
        )
        assert payload["ok"] is True
        assert payload["result"]["reap"] == {
            "status": "failed",
            "reason": "kill_pane_failed",
            "pane": "%42",
            "pane_role": "mechanicus:1",
        }
    finally:
        server.shutdown()


class WrapperStartAdapter:
    """Singleton seat at %42 labelled council:custodes, voice unlocked.

    Records set-option writes so the wrapperstart contract (daemon-authoritative
    wrapper-ownership stamp + persona tint derived from the durable @PANE_ID
    label, NOT from @INSTANCE_ID) can be asserted without a live tmux.
    """

    def __init__(self) -> None:
        self.pane_label = "council:custodes"
        self.wrapper_owner = ""
        self.voice_lock = ""
        # Owner reported by the list-panes -a fallback (the local fast-path stamp
        # already on the pane), used when the payload omits tmux_pane so the
        # handler must resolve via _find_pane_by_wrapper_id.
        self.listed_owner = ""
        self.set_options: list[tuple[str, ...]] = []

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:3] == ("display-message", "-t", "%42"):
            return "%42"
        if args[:3] == ("list-panes", "-a", "-F"):
            return f"%42__TMUXCTLD_WRAPPEREND_FIELD__{self.listed_owner}"
        if args[:1] == ("set-option",):
            self.set_options.append(tuple(args))
            if "@TOKEN_API_WRAPPER_LAUNCH_ID" in args:
                self.wrapper_owner = args[-1]
            return ""
        if args[:2] == ("show-options", "-pqv") and "@DISCORD_VOICE_LOCK" in args:
            return self.voice_lock
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        if option == "@PANE_ID":
            return self.pane_label
        if option == "@TOKEN_API_WRAPPER_LAUNCH_ID":
            return self.wrapper_owner
        if option == "@DISCORD_VOICE_LOCK":
            return self.voice_lock
        return ""

    def clear_runtime_state(self, target: str) -> None:
        # Existing wrapperstart tests care about the subsequent stamp/tint writes;
        # comprehensive scrub coverage lives in ComprehensiveWrapperEndAdapter.
        self.set_options.append(("clear_runtime_state", target))
        self.wrapper_owner = ""


def test_wrapperstart_stamps_wrapper_owner_and_paints_persona_tint() -> None:
    rec = WrapperStartAdapter()
    server, _ = _serve(lambda: rec)
    try:
        status, payload = _post(
            server,
            "/hooks/wrapperstart",
            {"wrapper_launch_id": "wrap-7", "tmux_pane": "%42"},
        )
        assert status == 200
        assert payload["ok"] is True
        assert payload["result"]["status"] == "stamped"
        assert payload["result"]["pane"] == "%42"
        # (1) Daemon-authoritative wrapper-ownership stamp landed.
        assert rec.wrapper_owner == "wrap-7"
        # (2) Custodes persona tint painted from the @PANE_ID label, with NO
        #     @INSTANCE_ID present — the empty-stamp-at-birth case.
        assert payload["result"]["tint"] == "#302800"
        tint_writes = [
            opt for opt in rec.set_options if "window-style" in opt and "bg=#302800" in opt
        ]
        assert tint_writes, f"expected a custodes tint write, got {rec.set_options}"
    finally:
        server.shutdown()


def test_wrapperstart_resolves_pane_by_wrapper_id_when_payload_pane_missing() -> None:
    # The hardening path: no tmux_pane in the payload (stale/missing TMUX_PANE),
    # so _h_hook_wrapperstart must fall back to _find_pane_by_wrapper_id and still
    # stamp + tint the resolved seat.
    rec = WrapperStartAdapter()
    rec.listed_owner = "wrap-10"  # local fast-path stamp already on %42
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/hooks/wrapperstart",
            {"wrapper_launch_id": "wrap-10"},
        )
        assert payload["ok"] is True
        assert payload["result"]["status"] == "stamped"
        assert payload["result"]["pane"] == "%42"  # resolved by wrapper id
        assert rec.wrapper_owner == "wrap-10"  # re-affirmed by the daemon
        assert payload["result"]["tint"] == "#302800"  # custodes tint still painted
        tint_writes = [
            opt for opt in rec.set_options if "window-style" in opt and "bg=#302800" in opt
        ]
        assert tint_writes, f"expected a custodes tint write, got {rec.set_options}"
    finally:
        server.shutdown()


def test_wrapperstart_skips_tint_for_non_persona_pane() -> None:
    rec = WrapperStartAdapter()
    rec.pane_label = "mechanicus:3"  # a stack worker, not a tinted singleton seat
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/hooks/wrapperstart",
            {"wrapper_launch_id": "wrap-8", "tmux_pane": "%42"},
        )
        assert payload["ok"] is True
        assert payload["result"]["status"] == "stamped"
        assert rec.wrapper_owner == "wrap-8"  # wrapper stamp still lands
        assert payload["result"]["tint"] == ""  # but no persona tint
        # And no tmux styling was mutated — guards against a handler that paints
        # but forgets to report the tint (mirrors the voice-lock coverage).
        assert not [opt for opt in rec.set_options if "window-style" in opt]
    finally:
        server.shutdown()


def test_wrapperstart_honors_discord_voice_lock() -> None:
    rec = WrapperStartAdapter()
    rec.voice_lock = "1"
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/hooks/wrapperstart",
            {"wrapper_launch_id": "wrap-9", "tmux_pane": "%42"},
        )
        assert payload["ok"] is True
        assert payload["result"]["tint"] == ""  # voice lock wins over persona tint
        assert not [opt for opt in rec.set_options if "window-style" in opt]
    finally:
        server.shutdown()


def test_wrapperstart_requires_wrapper_launch_id() -> None:
    rec = WrapperStartAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(server, "/hooks/wrapperstart", {"tmux_pane": "%42"})
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ValueError"
    finally:
        server.shutdown()


def test_wrapper_ledger_upsert_resolves_triple_id_and_reloads_json(tmp_path, monkeypatch) -> None:
    path = tmp_path / "ledger.json"
    monkeypatch.setenv("TMUXCTLD_WRAPPER_LEDGER_PATH", str(path))
    wrapper_ledger.LEDGER.load(force=True)

    server, _ = _serve(StubAdapter)
    try:
        _, upserted = _post(
            server,
            "/ledger/upsert",
            {
                "wrapper_id": "wrap-core",
                "instance_id": "inst-core",
                "persona": "custodes",
                "pane_positional_id": "council:custodes",
                "engine": "codex",
                "working_dir": "/tmp/core",
                "state": "OPEN",
            },
        )
        assert upserted["ok"] is True
        assert path.exists()

        resolved = []
        for query in (
            "/ledger/resolve?wrapper_id=wrap-core",
            "/ledger/resolve?instance_id=inst-core",
            "/ledger/resolve?pane_positional_id=council:custodes",
        ):
            _, payload = _get(server, query)
            assert payload["ok"] is True
            assert payload["result"]["found"] is True
            resolved.append(payload["result"]["row"])
        assert resolved[0] == resolved[1] == resolved[2]
    finally:
        server.shutdown()

    # Simulate daemon boot: a fresh load reconstructs the in-memory indexes from
    # the write-behind JSON, before any live tmux reconcile scan runs.
    wrapper_ledger.LEDGER._rows = {}
    wrapper_ledger.LEDGER._loaded = False
    wrapper_ledger.LEDGER.load(force=True)
    assert wrapper_ledger.LEDGER.resolve(instance_id="inst-core").pane_positional_id == (
        "council:custodes"
    )


class ReconcileLedgerAdapter(StubAdapter):
    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:3] == ("list-panes", "-a", "-F"):
            sep = wrapper_ledger._SCAN_SEP
            return sep.join(
                [
                    "wrap-live",
                    "inst-live",
                    "fabricator-general",
                    "mechanicus:fabricator-general",
                    "codex",
                    "/tmp/live",
                    "123.5",
                ]
            )
        return ""


def test_reconcile_rebuilds_wrapper_ledger_from_tmux_scan(tmp_path, monkeypatch) -> None:
    path = tmp_path / "ledger.json"
    monkeypatch.setenv("TMUXCTLD_WRAPPER_LEDGER_PATH", str(path))
    wrapper_ledger.LEDGER.load(force=True)
    wrapper_ledger.LEDGER.upsert(
        wrapper_id="stale-open",
        instance_id="stale-inst",
        pane_positional_id="palace:W",
        state="OPEN",
    )

    server, _ = _serve(ReconcileLedgerAdapter)
    try:
        _, payload = _post(server, "/reconcile", {})
        assert payload["ok"] is True
        assert payload["result"]["ledger"]["open_rows"] == 1
        assert payload["result"]["ledger"]["pruned_open_rows"] == 1
        _, resolved = _get(server, "/ledger/resolve?instance_id=inst-live")
        assert resolved["result"]["row"]["wrapper_id"] == "wrap-live"
        assert resolved["result"]["row"]["pane_positional_id"] == "mechanicus:fabricator-general"
        _, stale = _get(server, "/ledger/resolve?instance_id=stale-inst")
        assert stale["result"]["found"] is False
    finally:
        server.shutdown()


class RecordingFocusAdapter:
    """Resolves focus-uuid -> palace:1 and records every run() call."""

    def __init__(self) -> None:
        self.calls = []

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(args)
        if args[:2] == ("list-panes", "-a"):
            return "%24\tfocus-uuid\tpalace:1"
        if args and args[0] == "display-message" and "#{session_name}:#{window_index}" in args:
            return "main:3"
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.run("show-options", "-pv", "-t", pane_id, option, allow_failure=True).strip()


def test_instance_focus_honors_explicit_client() -> None:
    rec = RecordingFocusAdapter()
    server, _ = _serve(lambda: rec)
    try:
        status, payload = _post(
            server, "/instance/focus", {"instance_id": "focus-uuid", "client": "viewer-1"}
        )
        assert status == 200
        assert payload["result"]["found"] is True
        # The explicit client is pointed at the pane's window before select-pane.
        assert ("switch-client", "-c", "viewer-1", "-t", "main:3") in rec.calls
    finally:
        server.shutdown()


def test_translate_ids_unresolved_passthrough() -> None:
    server, _ = _serve(StubAdapter)
    try:
        status, payload = _post(server, "/translate-ids", {"text": "pane %9 here"})
        assert status == 200
        assert payload["ok"] is True
        # No live mapping -> raw id is replaced with the fail-closed sentinel.
        assert payload["result"] == "pane unresolved here"
    finally:
        server.shutdown()


def test_unknown_route_is_404_envelope() -> None:
    server, _ = _serve(StubAdapter)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/nope"
        try:
            urllib.request.urlopen(url, timeout=5)
            raise AssertionError("expected HTTP 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            body = json.loads(exc.read().decode("utf-8"))
            assert body["ok"] is False
            assert body["error"]["code"] == "not_found"
    finally:
        server.shutdown()


def test_bad_json_body_is_400() -> None:
    server, _ = _serve(StubAdapter)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/send-text"
        req = urllib.request.Request(
            url, data=b"{not json", headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected HTTP 400")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            body = json.loads(exc.read().decode("utf-8"))
            assert body["error"]["code"] == "bad_request"
    finally:
        server.shutdown()


class SendAckAdapter:
    """Pane carries an instance stamp; send-keys calls are recorded."""

    calls: list[tuple[str, ...]] = []
    # Set once the literal `send-keys -l` injection is recorded, so the test can
    # post its ack deterministically AFTER the send started (no sleep race).
    literal_sent: threading.Event | None = None

    def __init__(self) -> None:
        self.last_send_gate_result = None

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        type(self).calls.append(tuple(args))
        if args[:1] == ("send-keys",) and "-l" in args and type(self).literal_sent is not None:
            type(self).literal_sent.set()
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.run("send-keys", "-t", target, *keys, allow_failure=allow_failure)

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return "inst-ack" if option == "@INSTANCE_ID" else ""


def test_send_text_waits_for_user_prompt_submit_ack() -> None:
    SendAckAdapter.calls = []
    SendAckAdapter.literal_sent = threading.Event()
    server, _ = _serve(SendAckAdapter)
    try:
        result_box: dict = {}

        def send() -> None:
            result_box["status"], result_box["payload"] = _post_timeout(
                server,
                "/send-text",
                {
                    "pane": "%42",
                    "text": "do the thing",
                    "verify": True,
                    "verify_timeout": 2,
                    "submit_settle_seconds": 0.01,
                },
                timeout=5,
            )

        thread = threading.Thread(target=send)
        thread.start()
        # Wait for the literal send to be recorded before posting the ack, so the
        # sniffer's `since` window is already open and can never drop it as stale.
        assert SendAckAdapter.literal_sent is not None
        assert SendAckAdapter.literal_sent.wait(timeout=2)
        _, ack = _post(
            server,
            "/hooks/user-prompt-submit",
            {"session_id": "inst-ack"},
        )
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert ack["ok"] is True
        payload = result_box["payload"]
        assert payload["ok"] is True
        result = payload["result"]
        assert result["verification_status"] == "submitted"
        assert result["verified_by"] == "UserPromptSubmit"
        assert result["instance_id"] == "inst-ack"
        assert ("send-keys", "-t", "%42", "-l", "do the thing") in SendAckAdapter.calls
        assert ("send-keys", "-t", "%42", "C-m") in SendAckAdapter.calls
    finally:
        server.shutdown()


def test_send_text_reports_unverified_without_prompt_submit_ack() -> None:
    SendAckAdapter.calls = []
    server, _ = _serve(SendAckAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "do the thing",
                "verify": True,
                "verify_timeout": 0.01,
                "submit_settle_seconds": 0,
            },
            timeout=5,
        )
        assert status == 200
        assert payload["ok"] is True
        result = payload["result"]
        assert result["verification_status"] == "unverified"
        assert result["verified_by"] is None
    finally:
        server.shutdown()


class RecoveryClearsDraftAdapter(SendAckAdapter):
    """First verify sees a stuck draft; recovery C-m clears it."""

    capture_calls = 0

    def capture_pane(self, pane_id: str, *, lines: int = 10) -> str:
        type(self).capture_calls += 1
        return (
            "do the thing\n" if type(self).capture_calls == 1 else "submitted prompt left composer"
        )

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return "inst-recovered" if option == "@INSTANCE_ID" else ""


def test_send_text_credits_recovery_path_submit_when_draft_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swallowed Enter recovered by C-m is a submitted send, not unverified."""
    RecoveryClearsDraftAdapter.calls = []
    RecoveryClearsDraftAdapter.capture_calls = 0
    monkeypatch.setattr(daemon, "_notify_swallowed_submit", lambda **_kwargs: None)
    server, _ = _serve(RecoveryClearsDraftAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "do the thing",
                "verify": True,
                "verify_timeout": 0.01,
                "submit_settle_seconds": 0,
                "ack_submit_retries": 1,
            },
            timeout=5,
        )
        assert status == 200
        result = payload["result"]
        assert result["verification_status"] == "submitted"
        assert result["verified_by"] == "UserPromptSubmit"
        assert result["swallowed_submit_detected"] is True
        assert result["recovery_attempts"] == 1
        physical_submits = [
            c for c in RecoveryClearsDraftAdapter.calls if c == ("send-keys", "-t", "%42", "C-m")
        ]
        assert len(physical_submits) == 3  # initial submit pair + one recovery C-m
    finally:
        server.shutdown()


class CountingSendAdapter(SendAckAdapter):
    literal_count = 0

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:1] == ("send-keys",) and "-l" in args:
            type(self).literal_count += 1
        return super().run(*args, allow_failure=allow_failure)


def test_send_text_operation_id_is_idempotent_after_unverified_result() -> None:
    """Retrying the same per-id operation must not issue bytes twice."""
    CountingSendAdapter.calls = []
    CountingSendAdapter.literal_count = 0
    server, _ = _serve(CountingSendAdapter)
    body = {
        "pane": "%42",
        "text": "do the thing",
        "verify": True,
        "verify_timeout": 0.01,
        "submit_settle_seconds": 0,
        "operation_id": "op-regression-477",
    }
    try:
        first_status, first_payload = _post_timeout(server, "/send-text", body, timeout=5)
        second_status, second_payload = _post_timeout(server, "/send-text", body, timeout=5)
        assert first_status == second_status == 200
        assert first_payload["result"]["verification_status"] == "unverified"
        assert second_payload["result"]["verification_status"] == "unverified"
        assert second_payload["result"]["idempotent_replay"] is True
        assert CountingSendAdapter.literal_count == 1
    finally:
        server.shutdown()


def test_invoke_skill_submit_uses_shared_send_core_with_codex_tab() -> None:
    SendAckAdapter.calls = []
    server, _ = _serve(SendAckAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/invoke-skill",
            {
                "pane": "%42",
                "name": "golden-throne-sop",
                "kind": "skill",
                "agent": "codex",
                "arguments": "needs tests",
                "submit": True,
                "verify": False,
                "submit_settle_seconds": 0,
            },
            timeout=5,
        )
        assert status == 200
        assert payload["ok"] is True
        result = payload["result"]
        assert result["kind"] == "skill"
        assert result["rendered"] == "$golden-throne-sop needs tests"
        assert result["dispatch_id"]
        assert (
            "send-keys",
            "-t",
            "%42",
            "-l",
            "$golden-throne-sop needs tests",
        ) in SendAckAdapter.calls
        assert ("send-keys", "-t", "%42", "Tab") in SendAckAdapter.calls
    finally:
        server.shutdown()


def test_invoke_command_submit_never_gets_skill_tab() -> None:
    SendAckAdapter.calls = []
    server, _ = _serve(SendAckAdapter)
    try:
        _, payload = _post_timeout(
            server,
            "/invoke-skill",
            {
                "pane": "%42",
                "name": "plan",
                "kind": "command",
                "agent": "codex",
                "submit": True,
                "verify": False,
                "submit_settle_seconds": 0,
            },
            timeout=5,
        )
        assert payload["ok"] is True
        assert payload["result"]["rendered"] == "/plan "
        assert ("send-keys", "-t", "%42", "Tab") not in SendAckAdapter.calls
    finally:
        server.shutdown()


def test_send_ethereal_renders_claude_btw_and_closes_side_channel() -> None:
    rec = RecordingVoiceAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post_timeout(
            server,
            "/send-ethereal",
            {
                "pane": "%42",
                "agent": "claude",
                "message": "roll call",
                "verify": False,
                "submit_settle_seconds": 0,
            },
            timeout=5,
        )
        assert payload["ok"] is True
        assert payload["result"]["rendered"] == "/btw roll call"
        assert ("send-keys", "-t", "%42", "-l", "/btw roll call") in rec.calls
        assert ("send-keys-helper", "%42", "c") in rec.calls
        assert ("send-keys-helper", "%42", "C-c") in rec.calls
    finally:
        server.shutdown()


def test_send_ethereal_renders_codex_side_copy_and_closes_side_channel() -> None:
    rec = RecordingVoiceAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post_timeout(
            server,
            "/send-ethereal",
            {
                "pane": "%42",
                "agent": "codex",
                "message": "roll call",
                "verify": False,
                "submit_settle_seconds": 0,
            },
            timeout=5,
        )
        assert payload["ok"] is True
        assert payload["result"]["rendered"] == "/side roll call"
        assert ("send-keys", "-t", "%42", "-l", "/side roll call") in rec.calls
        assert ("send-keys", "-t", "%42", "-l", "/copy") in rec.calls
        assert ("send-keys-helper", "%42", "C-c") in rec.calls
    finally:
        server.shutdown()


def test_append_user_text_inserts_without_clear_or_enter() -> None:
    rec = RecordingVoiceAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/append-user-text",
            {"pane": "%42", "text": "[discord] hello"},
        )
        assert payload["ok"] is True
        assert payload["result"]["direct_user"] is True
        assert rec.calls == [("send-keys", "-t", "%42", "-l", "[discord] hello")]
    finally:
        server.shutdown()


def test_serve_refuses_non_loopback_bind() -> None:
    # The daemon is unauthenticated and does powerful tmux ops — serve() must
    # fail closed (no bind) on any non-loopback host.
    assert daemon.serve("0.0.0.0", 0) == 2
    assert daemon.serve("10.0.0.5", 0) == 2


def test_malformed_content_length_is_bad_request() -> None:
    # A non-integer Content-Length must normalize to a 400 bad_request envelope,
    # never an unhandled 500. Crafted over a raw socket (urllib would rewrite the
    # header), with Connection: close so the server replies once and hangs up.
    server, _ = _serve(StubAdapter)
    try:
        host, port = server.server_address
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(
                b"POST /send-text HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: not-a-number\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        raw = b"".join(chunks).decode("utf-8", "ignore")
        status_line, _, body = raw.partition("\r\n\r\n")
        assert "400" in status_line.splitlines()[0]
        assert json.loads(body)["error"]["code"] == "bad_request"
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Agent-guard hold (B1) + loud swallowed-submit recovery (B2)
# ---------------------------------------------------------------------------


class StuckDraftAdapter:
    """send-keys recorded; capture-pane returns a stuck draft (Enter swallowed).

    The composer still holds the payload head AND ends in a trailing newline —
    the white-whale signature ``_detect_swallowed_submit`` matches. The sniffer
    is never fed an ack, so the verify loop exhausts its retries.
    """

    calls: list[tuple[str, ...]] = []

    def __init__(self) -> None:
        self.last_send_gate_result = None

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        type(self).calls.append(tuple(args))
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.run("send-keys", "-t", target, *keys, allow_failure=allow_failure)

    def capture_pane(self, pane_id: str, *, lines: int = 10) -> str:
        return "do the thing\n"

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return "inst-stuck" if option == "@INSTANCE_ID" else ""


def test_agent_guard_hold_acquired_and_released_even_when_verify_times_out(monkeypatch) -> None:
    # The hold is taken before the send and released in `finally` even when no
    # ack ever arrives (verify times out) — the guard must never leak green.
    SendAckAdapter.calls = []
    events: list[str] = []
    monkeypatch.setattr(
        daemon.typing_guard_state, "hold", lambda *a, **k: events.append("hold") or "req-1"
    )
    monkeypatch.setattr(
        daemon.typing_guard_state, "release", lambda *a, **k: events.append("release")
    )
    server, _ = _serve(SendAckAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "do the thing",
                "verify": True,
                "verify_timeout": 0.01,
                "submit_settle_seconds": 0,
            },
            timeout=5,
        )
        assert status == 200
        result = payload["result"]
        assert result["guard_held"] is True
        assert result["verification_status"] == "unverified"
        assert events == ["hold", "release"], "hold acquired, then released in finally"
    finally:
        server.shutdown()


def test_agent_guard_hold_denied_does_not_release_and_send_still_routes(monkeypatch) -> None:
    # A live human lock denies the hold (held=False, the autouse default). The
    # daemon must NOT force/release — the send still routes (through the normal
    # gate, which would delay behind the human) and guard_held is False.
    SendAckAdapter.calls = []
    released: list[str] = []
    monkeypatch.setattr(
        daemon.typing_guard_state, "release", lambda *a, **k: released.append("release")
    )
    server, _ = _serve(SendAckAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "do the thing",
                "verify": True,
                "verify_timeout": 0.01,
                "submit_settle_seconds": 0,
            },
            timeout=5,
        )
        assert status == 200
        result = payload["result"]
        assert result["guard_held"] is False
        assert released == [], "a denied hold must never call release"
        assert ("send-keys", "-t", "%42", "-l", "do the thing") in SendAckAdapter.calls
    finally:
        server.shutdown()


def test_send_text_returns_gated_without_waiting_or_writing_under_typing_guard(monkeypatch) -> None:
    SendAckAdapter.calls = []
    hold_calls: list[str] = []
    monkeypatch.setattr(
        daemon.send_gate,
        "evaluate",
        lambda *a, **k: {
            "suppressed": True,
            "policy": "delay",
            "reason": "typing_guard",
            "target": "%42",
        },
    )
    monkeypatch.setattr(
        daemon.typing_guard_state, "hold", lambda *a, **k: hold_calls.append("hold") or "req-1"
    )
    server, _ = _serve(SendAckAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "do the thing",
                "verify": True,
                "verify_timeout": 5,
                "submit_settle_seconds": 0,
            },
            timeout=1,
        )
        assert status == 200
        assert payload["ok"] is False
        assert payload["error"]["code"] == "gated"
        assert payload["error"]["detail"]["reason"] == "typing_guard"
        assert payload["error"]["detail"]["deferred"] is True
        assert hold_calls == [], "do not acquire agent hold behind a live human guard"
        assert SendAckAdapter.calls == [], "zero bytes issued while human guard is active"
    finally:
        server.shutdown()


def test_insert_only_send_text_is_also_gated_under_typing_guard(monkeypatch) -> None:
    SendAckAdapter.calls = []
    monkeypatch.setattr(
        daemon.send_gate,
        "evaluate",
        lambda *a, **k: {
            "suppressed": True,
            "policy": "delay",
            "reason": "typing_guard",
            "target": "%42",
        },
    )
    server, _ = _serve(SendAckAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "insert only",
                "submit": False,
                "verify": False,
                "submit_settle_seconds": 0,
            },
            timeout=1,
        )
        assert status == 200
        assert payload["ok"] is False
        assert payload["error"]["code"] == "gated"
        assert SendAckAdapter.calls == []
    finally:
        server.shutdown()


@pytest.mark.parametrize(
    "capture,payload,expected",
    [
        ("do the thing\n", "do the thing", True),  # draft present + trailing newline
        ("$ prompt> do the thing\n", "do the thing", True),  # head present inside composer
        ("do the thing", "do the thing", False),  # no trailing newline → submitted/clean line
        ("", "do the thing", False),  # empty composer → clean submit
        ("unrelated shell output\n", "do the thing", False),  # payload absent
        ("do the thing\n", "", False),  # empty payload never matches
    ],
)
def test_detect_swallowed_submit(capture: str, payload: str, expected: bool) -> None:
    assert daemon._detect_swallowed_submit(capture, payload) is expected


def test_swallowed_submit_fires_recovery_and_surfaces_loudly(monkeypatch) -> None:
    # The white-whale: bytes landed, Enter swallowed. The daemon must STILL fire
    # the recovery C-m (sink the stuck draft) AND surface the failure loudly —
    # never silently eat it.
    StuckDraftAdapter.calls = []
    notified: list[dict] = []
    monkeypatch.setattr(daemon.typing_guard_state, "hold", lambda *a, **k: "req-1")
    monkeypatch.setattr(daemon.typing_guard_state, "release", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_notify_swallowed_submit", lambda **kw: notified.append(kw))
    server, _ = _serve(StuckDraftAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "do the thing",
                "verify": True,
                "verify_timeout": 0.01,
                "ack_submit_retries": 1,
                "submit_settle_seconds": 0,
            },
            timeout=5,
        )
        assert status == 200
        result = payload["result"]
        assert result["swallowed_submit_detected"] is True
        assert result["recovery_attempts"] >= 1
        assert any(f["type"] == "swallowed_submit" for f in result["failures"])
        # Recovery C-m still fired to sink the stuck draft.
        assert ("send-keys", "-t", "%42", "C-m") in StuckDraftAdapter.calls
        # And the failure was surfaced on the notify path, not eaten.
        assert notified, "swallowed submit must be surfaced via notify"
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Discord voice-session API
# ---------------------------------------------------------------------------


def test_voice_start_returns_opaque_id_and_public_role_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = RecordingVoiceAdapter()
    monkeypatch.setattr(daemon, "_voice_resolve_target", lambda _control, _bot: "palace:E")
    server, _ = _serve(lambda: rec)
    session_id = None
    try:
        status, payload = _post(
            server,
            "/voice/session/start",
            {
                "bot_name": "imperial_guard",
                "user_id": "operator",
                "channel_id": "cadia",
                "route_epoch": 7,
            },
        )
        assert status == 200
        assert payload["ok"] is True
        result = payload["result"]
        session_id = result["voice_session_id"]
        assert result["target_role"] == "palace:E"
        assert session_id
        assert "%" not in json.dumps(payload)
        assert "pane" not in json.dumps(payload).lower()
    finally:
        server.shutdown()
        if session_id:
            daemon.VOICE_SESSIONS.remove(session_id)


def test_voice_append_ship_scratch_clear_mutate_public_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = RecordingVoiceAdapter()
    monkeypatch.setattr(daemon, "_voice_resolve_target", lambda _control, _bot: "palace:E")
    server, _ = _serve(lambda: rec)
    created_session_ids: list[str] = []
    try:
        _, started = _post(
            server, "/voice/session/start", {"bot_name": "imperial_guard", "user_id": "u1"}
        )
        sid = started["result"]["voice_session_id"]
        created_session_ids.append(sid)
        _, appended = _post(
            server, "/voice/session/append", {"voice_session_id": sid, "text": "draft"}
        )
        assert appended["result"]["inserted"] is True
        assert ("send-keys", "-t", "palace:E", "-l", " ") in rec.calls
        assert ("send-keys", "-t", "palace:E", "-l", "draft") in rec.calls

        _, shipped = _post(
            server, "/voice/session/ship", {"voice_session_id": sid, "text": "final"}
        )
        assert shipped["result"]["shipped"] is True
        assert ("send-keys-helper", "palace:E", "Enter") in rec.calls

        _, started2 = _post(
            server, "/voice/session/start", {"bot_name": "imperial_guard", "user_id": "u1"}
        )
        sid2 = started2["result"]["voice_session_id"]
        created_session_ids.append(sid2)
        _, scratched = _post(server, "/voice/session/scratch", {"voice_session_id": sid2})
        assert scratched["result"]["scratched"] is True
        assert ("send-keys-helper", "palace:E", "C-c") in rec.calls

        _, started3 = _post(
            server, "/voice/session/start", {"bot_name": "imperial_guard", "user_id": "u1"}
        )
        sid3 = started3["result"]["voice_session_id"]
        created_session_ids.append(sid3)
        _, cleared = _post(server, "/voice/session/clear", {"voice_session_id": sid3})
        assert cleared["result"]["cleared"] == 1
    finally:
        server.shutdown()
        for session_id in created_session_ids:
            daemon.VOICE_SESSIONS.remove(session_id)


class ImperialGuardNoClientAdapter(StubAdapter):
    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:2] == ("list-clients", "-F"):
            return ""
        return ""


def test_voice_imperial_guard_fails_closed_with_no_routable_client() -> None:
    server, _ = _serve(ImperialGuardNoClientAdapter)
    try:
        _, payload = _post(
            server, "/voice/session/start", {"bot_name": "imperial_guard", "user_id": "u1"}
        )
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ValueError"
        assert "no routable" in payload["error"]["message"]
    finally:
        server.shutdown()


def test_voice_clear_by_bot_clears_stale_target_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = RecordingVoiceAdapter()
    monkeypatch.setattr(daemon, "_voice_resolve_target", lambda _control, _bot: "palace:E")
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(server, "/voice/session/clear", {"bot_name": "imperial_guard"})
        assert payload["result"]["cleared"] == 0
        assert payload["result"]["cleared_options"] is True
        assert ("set-option", "-p", "-t", "palace:E", "@DISCORD_VOICE_LOCK", "0") in rec.calls
    finally:
        server.shutdown()


def test_startup_installs_tmux_lifecycle_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Proc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Proc()

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon, "tmux_binary", lambda: "tmux")

    out = daemon.ensure_tmux_lifecycle_hooks()

    assert out["ok"] is True
    assert calls == [
        (
            ("tmux", "set-option", "-g", "remain-on-exit", "on"),
            {
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "check": False,
            },
        ),
        (
            ("tmux", "set-hook", "-g", "pane-died[90]", daemon._PANE_DIED_HOOK),
            {
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "check": False,
            },
        ),
    ]
    assert "tmuxctld-ping POST /event" in daemon._PANE_DIED_HOOK
    assert "pane=#{pane_id}" in daemon._PANE_DIED_HOOK
    assert "display-message" in daemon._PANE_DIED_HOOK
    assert "tmux-pane-respawn" not in daemon._PANE_DIED_HOOK


def test_not_implemented_anchor_returns_loud_http_501(monkeypatch: pytest.MonkeyPatch) -> None:
    def anchor(_control, _params):
        return daemon.not_implemented_anchor(
            "POST", "/future-anchor", detail="daemon-native replacement not built yet"
        )

    monkeypatch.setitem(daemon.ROUTES, ("POST", "/future-anchor"), anchor)
    server, _ = _serve(StubAdapter)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(server, "/future-anchor", {})
        assert excinfo.value.code == 501
        payload = json.loads(excinfo.value.read().decode("utf-8"))
        assert payload["ok"] is False
        assert payload["error"]["code"] == "not_implemented"
        assert payload["error"]["detail"]["method"] == "POST"
        assert payload["error"]["detail"]["path"] == "/future-anchor"
    finally:
        server.shutdown()


def test_startup_lifecycle_hook_install_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args, **_kwargs):
        raise PermissionError("tmux denied")

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)

    out = daemon.ensure_tmux_lifecycle_hooks()

    assert out["ok"] is False
    assert len(out["results"]) == 2
    assert all(result["returncode"] is None for result in out["results"])


# ---------------------------------------------------------------------------
# Keybind daemon endpoints
# ---------------------------------------------------------------------------


class KeybindAdapter(StubAdapter):
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.zoomed = "0"
        self.capture = ""
        self.instance_id = "inst-keybind"

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(tuple(args))
        if args and args[0] == "resize-pane":
            self.zoomed = "1" if self.zoomed == "0" else "0"
            return ""
        if args == ("display-message", "-p", "#{pane_id}"):
            return "%42"
        if args[:3] == ("display-message", "-t", "%42") and "#{@PANE_ID}" in args[-1]:
            return f"%42\tpalace:N\tmain:1\t{self.zoomed}"
        if args == ("display-message", "-t", "%42", "-p", "#{pane_id}"):
            return "%42"
        if args == ("display-message", "-t", "%42", "-p", "#{session_name}:#{window_index}"):
            return "main:1"
        if args == ("display-message", "-t", "main:1", "-p", "#{window_zoomed_flag}"):
            return self.zoomed
        if args == ("capture-pane", "-t", "%42", "-p", "-S", "-5"):
            return self.capture
        if args == ("display-message", "-p", "-t", "%42", "#{session_name}:#{window_id}"):
            return "main:@1"
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.run("send-keys", "-t", target, *keys, allow_failure=allow_failure)

    def show_pane_option(self, pane_id: str, option: str) -> str:
        if option == "@INSTANCE_ID":
            return self.instance_id
        if option == "@PANE_ID":
            return "palace:N"
        return ""


def test_grid_expand_endpoint_uses_native_zoom_and_clears_legacy_flags() -> None:
    rec = KeybindAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(server, "/grid-expand", {"pane": "%42", "action": "expand"})
        assert payload["ok"] is True
        assert payload["result"]["status"] == "ok"
        assert payload["result"]["action"] == "expand"
        assert payload["result"]["zoomed_after"] is True
        assert ("resize-pane", "-Z", "-t", "%42") in rec.calls
        assert ("set-option", "-w", "-t", "main:1", "@GRID_EXPANDED", "none") in rec.calls
        assert ("set-option", "-w", "-t", "main:1", "@GRID_STASH", "") in rec.calls
    finally:
        server.shutdown()


def test_mode_toggle_endpoint_detects_plan_and_sends_one_shift_tab() -> None:
    rec = KeybindAdapter()
    rec.capture = "status: plan mode on"
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(server, "/mode-toggle", {"pane": "%42", "delay": 0})
        assert payload["ok"] is True
        assert payload["result"]["from"] == "plan"
        assert payload["result"]["to"] == "bypass"
        assert payload["result"]["presses"] == 1
        assert ("send-keys", "-t", "%42", "BTab") in rec.calls
    finally:
        server.shutdown()


def test_open_session_doc_endpoint_posts_token_api_open_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(
        tmux_service,
        "_token_api_json",
        lambda method, path, body=None, **_kw: (
            calls.append((method, path, body)) or {"title": "Session One"}
        ),
    )
    server, _ = _serve(KeybindAdapter)
    try:
        _, payload = _post(server, "/open-session-doc", {"arg": "123"})
        assert payload["ok"] is True
        assert payload["result"]["doc_id"] == 123
        assert payload["result"]["title"] == "Session One"
        assert calls == [("POST", "/api/session-docs/123/open", None)]
    finally:
        server.shutdown()


def test_open_session_doc_endpoint_normalizes_pane_before_doc_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tmux_service.TmuxControlPlane,
        "public_pane_id",
        lambda _self, target: "palace:N" if target == "physical-pane" else "unresolved",
    )
    monkeypatch.setattr(
        tmux_service,
        "fetch_session_doc_for_pane_label",
        lambda pane_label: {"id": 456, "title": "Normalized", "pane_label": pane_label},
    )
    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(
        tmux_service,
        "_token_api_json",
        lambda method, path, body=None, **_kw: (
            calls.append((method, path, body)) or {"title": "Normalized"}
        ),
    )
    server, _ = _serve(KeybindAdapter)
    try:
        _, payload = _post(server, "/open-session-doc", {"pane": "physical-pane"})
        assert payload["ok"] is True
        assert payload["result"]["doc_id"] == 456
        assert payload["result"]["pane_label"] == "palace:N"
        assert calls == [("POST", "/api/session-docs/456/open", None)]
    finally:
        server.shutdown()


def test_pane_rename_empty_name_sends_interview_nudge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(
        tmux_service,
        "_token_api_json",
        lambda method, path, body=None, **_kw: (
            calls.append((method, path, body)) or {"success": True}
        ),
    )
    rec = KeybindAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(server, "/pane-rename", {"pane": "%42", "name": ""})
        assert payload["ok"] is True
        assert payload["result"]["status"] == "nudged"
        assert calls == [
            ("POST", "/api/orchestrator/naming_nudge", {"instance_id": "inst-keybind"})
        ]
    finally:
        server.shutdown()


def test_pane_rename_explicit_name_is_loud_501() -> None:
    server, _ = _serve(KeybindAdapter)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(server, "/pane-rename", {"pane": "%42", "name": "new-name"})
        assert excinfo.value.code == 501
        payload = json.loads(excinfo.value.read().decode("utf-8"))
        assert payload["error"]["code"] == "not_implemented"
        assert payload["error"]["detail"]["path"] == "/pane-rename"
    finally:
        server.shutdown()


def test_goto_spoken_endpoint_focuses_latest_tts_pane(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "agents.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE events (
              id INTEGER PRIMARY KEY,
              instance_id TEXT,
              event_type TEXT,
              created_at TEXT
            );
            CREATE TABLE instances (id TEXT PRIMARY KEY, tmux_pane TEXT, name TEXT);
            INSERT INTO instances (id, tmux_pane, name) VALUES ('inst-tts', '%42', 'Speaker');
            INSERT INTO instances (id, tmux_pane, name) VALUES ('inst-old', '%43', 'Old');
            INSERT INTO events (id, instance_id, event_type, created_at)
              VALUES (1, 'inst-tts', 'tts_playing', datetime('now'));
            INSERT INTO events (id, instance_id, event_type, created_at)
              VALUES (2, 'inst-old', 'tts_playing', datetime('now', '-1 hour'));
            """
        )
        conn.commit()
    finally:
        conn.close()

    rec = KeybindAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(server, "/goto-spoken", {"db_path": str(db), "max_age_seconds": 600})
        assert payload["ok"] is True
        assert payload["result"]["status"] == "focused"
        assert payload["result"]["instance_id"] == "inst-tts"
        assert ("select-window", "-t", "main:@1") in rec.calls
        assert ("select-pane", "-t", "%42") in rec.calls
    finally:
        server.shutdown()


@pytest.mark.parametrize(
    "route",
    ["/shuttle", "/mark-for-close", "/reset", "/ethereal-prompt", "/tts/listen", "/legion-prompt"],
)
def test_deferred_keybind_routes_are_loud_501(route: str) -> None:
    server, _ = _serve(StubAdapter)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(server, route, {})
        assert excinfo.value.code == 501
        payload = json.loads(excinfo.value.read().decode("utf-8"))
        assert payload["error"]["code"] == "not_implemented"
        assert payload["error"]["detail"]["path"] == route
    finally:
        server.shutdown()
