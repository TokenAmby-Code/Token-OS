"""Unit tests for the event-driven persona reconcile surface.

`TmuxControlPlane.handle_event` / `.reconcile_personas` are the engine behind the
daemon `/event` and `/reconcile` routes that replaced the retired 2-min
assert-personas poll. These drive the routing logic directly (a persona pane-died
must re-seat; a non-must-fill pane no-ops; a reservist pane-died re-seats its
standby agent) plus a daemon HTTP smoke test for the wiring.
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import urllib.request
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "tmuxctld" / "lib"))

from tmuxctl import assertions, daemon, service
from tmuxctl.service import TmuxControlPlane


class FakeAdapter:
    def __init__(self, options: dict | None = None) -> None:
        self.options = options or {}
        self.last_send_gate_result = None

    def list_sessions(self) -> list:
        return []

    def run(self, *args, allow_failure: bool = False) -> str:
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.options.get(option, "")


def _control(options=None) -> TmuxControlPlane:
    return TmuxControlPlane(adapter=FakeAdapter(options or {}))


def test_pane_died_hook_targets_tmuxctld_event_route() -> None:
    assert "POST /event" in daemon._PANE_DIED_HOOK
    assert "event=pane-died" in daemon._PANE_DIED_HOOK
    assert "pane=#{pane_id}" in daemon._PANE_DIED_HOOK


def test_handle_event_ignores_unhandled_event_type() -> None:
    out = _control().handle_event("pane-focus-in", pane="council:custodes")
    assert out["action"] == "ignored"
    assert "unhandled_event" in out["reason"]


def test_handle_event_requires_a_pane() -> None:
    out = _control().handle_event("pane-died", pane="")
    assert out["ok"] is False
    assert out["reason"] == "no_pane"


def test_handle_event_unresolved_pane_is_benign() -> None:
    # A dead/missing pane that no longer resolves must not 500 — it's benign.
    def boom(*a, **k):
        raise ValueError("no such pane")

    control = _control()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", boom)
        out = control.handle_event("pane-died", pane="%999")
    assert out["ok"] is True
    assert out["action"] == "ignored"
    assert "unresolved_pane" in out["reason"]


def test_handle_event_persona_seat_reseats_via_assert_instance() -> None:
    control = _control({"@PANE_ID": "council:custodes", "@PANE_TYPE": "council"})
    calls = {}

    def fake_resolve(adapter, target, session_name=None):
        return SimpleNamespace(pane_id="%5", pane_role="council:custodes")

    def fake_assert(adapter, target, *, session=None):
        calls["target"] = target
        calls["session"] = session
        return {"ok": True, "action": "launched", "reason": "launched"}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        mp.setattr(assertions, "assert_instance", fake_assert)
        out = control.handle_event("pane-died", pane="council:custodes", session="main")

    assert calls["target"] == "council:custodes"
    assert calls["session"] == "main"
    assert out["ok"] is True
    assert out["action"] == "launched"
    assert out["pane_label"] == "council:custodes"


def test_handle_event_stack_worker_husk_is_culled() -> None:
    # A dispatched mechanicus worker carries @PANE_TYPE=stack-worker (FILL_IF_ROW).
    # When its pane dies (remain-on-exit husk), the pane-died event MUST cull the
    # husk — not leave it for a manual cull. This is the dead-pane reaping gap:
    # the husk graveyard accumulated because handle_event used to no-op stack
    # workers. It now routes them through the unified teardown router (WORKER cull).
    adapter = RouterAdapter(
        window_name="mechanicus",
        pane_dead=True,
        options={"@PANE_ID": "mechanicus:3", "@PANE_TYPE": "stack-worker"},
    )
    control = TmuxControlPlane(adapter=adapter)

    def fake_resolve(a, target, session_name=None):
        return SimpleNamespace(pane_id="%4", pane_role="mechanicus:3")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        out = control.handle_event("pane-died", pane="mechanicus:3")

    assert out["action"] == "culled"
    assert out["pane_class"] == "worker"
    assert adapter.killed is True


def test_handle_event_live_stack_worker_is_not_killed() -> None:
    # Defense-in-depth: reap_dead_husk only kills a pane tmux confirms dead. A
    # stack worker whose pane is still LIVE (pane-died misfire / race) must never
    # be a collateral kill.
    adapter = RouterAdapter(
        window_name="mechanicus",
        pane_dead=False,
        options={"@PANE_ID": "mechanicus:3", "@PANE_TYPE": "stack-worker"},
    )
    control = TmuxControlPlane(adapter=adapter)

    def fake_resolve(a, target, session_name=None):
        return SimpleNamespace(pane_id="%4", pane_role="mechanicus:3")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        out = control.handle_event("pane-died", pane="mechanicus:3")

    assert out["pane_class"] == "worker"
    assert adapter.killed is False  # live pane preserved


class RouterAdapter(FakeAdapter):
    """FakeAdapter that answers the teardown router's tmux probes + records effects."""

    def __init__(self, *, window_name: str, pane_dead: bool = True, options=None) -> None:
        super().__init__(options or {})
        self.window_name = window_name
        self._pane_dead = pane_dead
        self.exists = True
        self.cleared: list[str] = []
        self.calls: list[tuple] = []

    def clear_runtime_state(self, target: str) -> None:
        self.cleared.append(target)

    def run(self, *args, allow_failure: bool = False) -> str:
        self.calls.append(tuple(args))
        if args and args[-1] == "#{window_name}":
            return self.window_name
        if args and args[-1] == "#{pane_dead}":
            return "1" if self._pane_dead else "0"
        if args and args[-1] == "#{pane_id}":
            return args[2] if self.exists else ""
        if args[:2] == ("kill-pane", "-t"):
            self.exists = False
            return ""
        if args[:1] == ("respawn-pane",):
            self._pane_dead = False
            return ""
        return ""

    @property
    def killed(self) -> bool:
        return any(c[:1] == ("kill-pane",) for c in self.calls)

    @property
    def respawned(self) -> bool:
        return any(c[:1] == ("respawn-pane",) for c in self.calls)


def test_handle_event_palace_slot_is_cleared_in_place_not_culled() -> None:
    # A completed one-off worker in a pre-allocated palace slot: the slot is
    # cleared IN PLACE and revived — PRESERVED, NOT culled (the morning over-reap).
    adapter = RouterAdapter(
        window_name="palace", pane_dead=True, options={"@PANE_ID": "palace:N", "@PANE_TYPE": ""}
    )
    control = TmuxControlPlane(adapter=adapter)

    def fake_resolve(a, target, session_name=None):
        return SimpleNamespace(pane_id="%7", pane_role="palace:N")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        out = control.handle_event("pane-died", pane="palace:N", session="main")

    assert out["ok"] is True
    assert out["action"] == "cleared_in_place"
    assert out["pane_class"] == "slot"
    assert adapter.killed is False  # slot preserved
    assert adapter.respawned is True  # revived in place -> back to freelist
    assert adapter.cleared == ["%7"]


def test_handle_event_dynamic_worker_is_culled() -> None:
    adapter = RouterAdapter(
        window_name="mechanicus",
        pane_dead=True,
        options={"@PANE_ID": "mechanicus:7", "@PANE_TYPE": ""},
    )
    control = TmuxControlPlane(adapter=adapter)

    def fake_resolve(a, target, session_name=None):
        return SimpleNamespace(pane_id="%9", pane_role="mechanicus:7")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        out = control.handle_event("pane-died", pane="mechanicus:7")

    assert out["action"] == "culled"
    assert out["pane_class"] == "worker"
    assert adapter.killed is True


def test_handle_event_reservist_pane_died_reseats_standby_agent() -> None:
    # F3: a reservist seat is must-fill — its pane-died now RESEATS the standby
    # agent via assert_reservist_seat (was a `reservist_refill_followon` no-op).
    control = _control({"@PANE_ID": "reservists:civic", "@PANE_TYPE": "reservists"})
    calls = {}

    def fake_resolve(adapter, target, session_name=None):
        return SimpleNamespace(pane_id="%3", pane_role="reservists:civic")

    def fake_assert(adapter, target, *, session=None):
        calls["target"] = target
        calls["session"] = session
        return {"ok": True, "action": "launched", "reason": "launched"}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        mp.setattr(assertions, "assert_reservist_seat", fake_assert)
        out = control.handle_event("pane-died", pane="reservists:civic", session="main")

    assert calls["target"] == "reservists:civic"
    assert calls["session"] == "main"
    assert out["ok"] is True
    assert out["action"] == "launched"
    assert out["pane_label"] == "reservists:civic"


def test_handle_event_reservist_never_returns_refill_followon() -> None:
    # The retired no-op reason must be gone entirely — the pane-died reservist
    # branch now acts, so `reservist_refill_followon` may never surface again.
    control = _control({"@PANE_ID": "reservists:token-os", "@PANE_TYPE": "reservists"})

    def fake_resolve(adapter, target, session_name=None):
        return SimpleNamespace(pane_id="%4", pane_role="reservists:token-os")

    def fake_assert(adapter, target, *, session=None):
        return {"ok": True, "action": "launched", "reason": "launched"}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        mp.setattr(assertions, "assert_reservist_seat", fake_assert)
        out = control.handle_event("pane-died", pane="reservists:token-os")

    assert out["reason"] != "reservist_refill_followon"


def test_reconcile_personas_sweeps_personas_and_reservists() -> None:
    # F3: reconcile now returns the persona sweep PLUS the reservist sweep
    # (fill-on-absence = "keep the pulse") — 6 personas + 2 reservists.
    seen = {"persona": [], "reservist": []}

    def fake_persona_sweep(adapter, *, session=None):
        seen["persona"].append(session)
        return [{"ok": True, "pane_label": label} for label in assertions.PERSONA_LABELS]

    def fake_reservist_sweep(adapter, *, session=None):
        seen["reservist"].append(session)
        return [{"ok": True, "pane_label": label} for label in assertions.RESERVIST_LABELS]

    control = _control()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(assertions, "sweep_persona_panes", fake_persona_sweep)
        mp.setattr(assertions, "sweep_reservist_panes", fake_reservist_sweep)
        results = control.reconcile_personas(session="main")

    assert seen["persona"] == ["main"]
    assert seen["reservist"] == ["main"]
    assert len(results) == len(assertions.PERSONA_LABELS) + len(assertions.RESERVIST_LABELS)
    labels = {r["pane_label"] for r in results}
    assert "reservists:civic" in labels
    assert "reservists:token-os" in labels


# -- daemon HTTP wiring smoke -------------------------------------------------


def _serve(adapter_factory):
    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0), adapter_factory=adapter_factory, version="9.9.9", sha="deadbee"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert server.ready.wait(timeout=5)
    return server


def _post(server, path, body):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_event_route_envelope_for_unhandled_event() -> None:
    server = _serve(FakeAdapter)
    try:
        status, payload = _post(server, "/event", {"event": "pane-focus-in", "pane": "x"})
        assert status == 200
        assert payload["ok"] is True
        assert payload["result"]["action"] == "ignored"
    finally:
        server.shutdown()


def test_reconcile_route_fails_loud_when_all_targets_error() -> None:
    # With a stub adapter no seat resolves; every target row errors. The daemon
    # must not return top-level ok/healthy with an all-error reconcile payload.
    server = _serve(FakeAdapter)
    try:
        status, payload = _post(server, "/reconcile", {})
        assert status == 200
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ValueError"
        assert "every target row errored" in payload["error"]["message"]
    finally:
        server.shutdown()


# -- /reconcile husk-safety (wrapper ledger) ---------------------------------

from tmuxctl.wrapper_ledger import _SCAN_SEP, WrapperLedger  # noqa: E402


class _LedgerScanAdapter:
    """Adapter stub whose list-panes scan returns crafted 9-field ledger lines."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def run(self, *args, allow_failure: bool = False) -> str:
        if args[:2] == ("list-panes", "-a"):
            return "\n".join(self._lines)
        return ""


def _scan_line(*, wrapper_id: str, instance_id: str, pane_id: str, pane_dead: bool) -> str:
    # wrapper_id, legacy_wrapper_id, instance_id, persona, pane_positional_id,
    # engine, working_dir, born_epoch, pane_dead
    return _SCAN_SEP.join(
        [wrapper_id, "", instance_id, "", pane_id, "claude", "/tmp", "0", "1" if pane_dead else "0"]
    )


def test_reconcile_skips_dead_husk_and_never_reopens_hollow_row(tmp_path) -> None:
    # A dead remain-on-exit husk is still listed by `list-panes -a` and may still
    # carry a stale @TOKEN_API_WRAPPER_ID while its @INSTANCE_ID was scrubbed.
    # /reconcile must NOT re-derive an OPEN row for it (the split-brain the reaper
    # closes) — only the genuinely live wrapper survives as OPEN.
    ledger = WrapperLedger(path=tmp_path / "ledger.json")
    adapter = _LedgerScanAdapter(
        [
            _scan_line(
                wrapper_id="w-live", instance_id="i-live", pane_id="mechanicus:1", pane_dead=False
            ),
            _scan_line(wrapper_id="w-husk", instance_id="", pane_id="mechanicus:2", pane_dead=True),
        ]
    )
    out = ledger.reconcile_from_tmux(adapter)

    open_wrappers = {row.wrapper_id for row in ledger.rows(include_closed=False)}
    assert "w-live" in open_wrappers
    assert "w-husk" not in open_wrappers  # dead husk never becomes a hollow OPEN row
    assert out["open_rows"] == 1


def _raw_scan_line(
    *,
    wrapper_id: str,
    instance_id: str = "",
    persona: str = "",
    pane_id: str,
    engine: str = "",
    working_dir: str = "",
    pane_dead: bool = False,
) -> str:
    return _SCAN_SEP.join(
        [
            wrapper_id,
            "",
            instance_id,
            persona,
            pane_id,
            engine,
            working_dir,
            "0",
            "1" if pane_dead else "0",
        ]
    )


def test_reconcile_skips_live_hollow_wrapper_stamp_false_birth(tmp_path) -> None:
    """A stale wrapper id alone must not reconstruct an OPEN occupancy row.

    Palace/somnium fixed slots can retain @TOKEN_API_WRAPPER_ID after runtime
    scrub while all actual runtime fields are empty. Reconcile used to interpret
    that as a live OPEN row and block dispatch with ledger/sniff disagreement.
    """
    ledger = WrapperLedger(path=tmp_path / "ledger.json")
    out = ledger.reconcile_from_tmux(
        _LedgerScanAdapter(
            [
                _raw_scan_line(wrapper_id="w-hollow", pane_id="somnium:N"),
                _raw_scan_line(
                    wrapper_id="w-live",
                    instance_id="i-live",
                    persona="worker",
                    pane_id="mechanicus:1",
                    engine="codex",
                    working_dir="/tmp/live",
                ),
            ]
        )
    )

    open_rows = {row.wrapper_id: row for row in ledger.rows(include_closed=False)}
    assert "w-live" in open_rows
    assert "w-hollow" not in open_rows
    assert out["open_rows"] == 1


def test_wrapper_ledger_rejects_hollow_active_upsert_and_prunes_on_load(tmp_path) -> None:
    ledger = WrapperLedger(path=tmp_path / "ledger.json")
    with pytest.raises(ValueError, match="hollow active"):
        ledger.upsert(wrapper_id="w-hollow", pane_positional_id="somnium:N", state="OPEN")

    # Existing write-behind files may already contain hollow active rows. Loading
    # them must not re-index them as occupancy.
    path = tmp_path / "ledger.json"
    path.write_text(
        '{"version":1,"rows":[{"wrapper_id":"w-hollow","instance_id":"",'
        '"persona":"","pane_positional_id":"somnium:N","engine":"",'
        '"working_dir":"","born_epoch":1,"state":"OPEN"},'
        '{"wrapper_id":"w-live","instance_id":"i-live","persona":"",'
        '"pane_positional_id":"mechanicus:1","engine":"codex",'
        '"working_dir":"","born_epoch":1,"state":"OPEN"}]}\n',
        encoding="utf-8",
    )
    loaded = WrapperLedger(path=path)
    loaded.load(force=True)

    assert loaded.resolve(wrapper_id="w-hollow") is None
    assert loaded.resolve(pane_positional_id="somnium:N") is None
    assert loaded.resolve(wrapper_id="w-live") is not None


def test_reconcile_prunes_prior_open_row_when_pane_is_now_a_dead_husk(tmp_path) -> None:
    # A wrapper that was OPEN and whose pane has since died to a husk must be pruned,
    # not re-opened, on the next reconcile.
    ledger = WrapperLedger(path=tmp_path / "ledger.json")
    ledger.reconcile_from_tmux(
        _LedgerScanAdapter(
            [_scan_line(wrapper_id="w1", instance_id="i1", pane_id="mechanicus:5", pane_dead=False)]
        )
    )
    assert "w1" in {r.wrapper_id for r in ledger.rows(include_closed=False)}
    out = ledger.reconcile_from_tmux(
        _LedgerScanAdapter(
            [_scan_line(wrapper_id="w1", instance_id="", pane_id="mechanicus:5", pane_dead=True)]
        )
    )
    assert "w1" not in {r.wrapper_id for r in ledger.rows(include_closed=False)}
    assert out["open_rows"] == 0


def test_reconcile_scan_failure_preserves_existing_open_rows(tmp_path) -> None:
    """A transient tmux scan failure must not rewrite the active ledger empty."""
    ledger = WrapperLedger(path=tmp_path / "ledger.json")
    ledger.upsert(
        wrapper_id="w-live",
        instance_id="i-live",
        pane_positional_id="mechanicus:5",
        engine="codex",
        working_dir="/tmp",
    )

    class FailingAdapter:
        def run(self, *args, allow_failure: bool = False) -> str:  # noqa: ARG002
            raise RuntimeError("tmux unavailable")

    with pytest.raises(RuntimeError, match="tmux unavailable"):
        ledger.reconcile_from_tmux(FailingAdapter())

    row = ledger.resolve(pane_positional_id="mechanicus:5")
    assert row is not None
    assert row.wrapper_id == "w-live"
    assert row.state == "OPEN"


# -- close-pane canonical-id resolution (#314-class stale handle) -------------


def test_h_close_pane_resolves_canonical_id_before_close() -> None:
    # _h_close_pane must resolve a canonical id (mechanicus:1) to its physical %NN
    # before calling close_pane, so tmux gets a matching handle and `already_closed`
    # is truthful — the reaper must not believe a still-live pane was reaped.
    seen = {}

    class _Ctrl:
        adapter = object()

        def close_pane(self, pane, *, timeout=3.0):
            seen["pane"] = pane
            seen["timeout"] = timeout
            return {"status": "closed", "pane": pane}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(daemon, "resolve_to_physical", lambda adapter, pane: "%42")
        out = daemon._h_close_pane(_Ctrl(), {"pane": "mechanicus:1", "timeout": "2.5"})

    assert seen["pane"] == "%42"  # resolved, not the raw canonical id
    assert seen["timeout"] == 2.5
    assert out["status"] == "closed"


def test_h_close_pane_passes_physical_id_through_untouched() -> None:
    seen = {}

    class _Ctrl:
        adapter = object()

        def close_pane(self, pane, *, timeout=3.0):
            seen["pane"] = pane
            return {"status": "closed", "pane": pane}

    # A raw %NN needs no resolution and must not be gated.
    daemon._h_close_pane(_Ctrl(), {"pane": "%7"})
    assert seen["pane"] == "%7"


def test_reconcile_route_fails_loud_when_every_target_errors(monkeypatch) -> None:
    class _Ctrl:
        def ledger_reconcile(self):
            return {"rows": 0}

        def reconcile_personas(self, session="main"):
            return [
                {"ok": False, "error": "tmux unavailable"},
                {"ok": False, "error": "tmux unavailable"},
            ]

    with pytest.raises(ValueError, match="every target row errored"):
        daemon._h_reconcile(_Ctrl(), {})


def test_h_close_pane_typing_guard_enqueues_close_operation(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TMUXCTLD_DEFERRED_SENDS_PATH", str(tmp_path / "deferred-sends.json"))
    monkeypatch.setattr(daemon, "_DEFERRED_SEND_QUEUE", daemon.DeferredSendQueue())
    monkeypatch.setattr(daemon, "_schedule_deferred_drain", lambda _pane: None)
    monkeypatch.setattr(daemon.send_gate, "_pane_human_locked", lambda _phys: True)

    class _Ctrl:
        adapter = object()

        def close_pane(self, pane, *, timeout=3.0):
            raise AssertionError("must not close while typing guard is active")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(daemon, "resolve_to_physical", lambda adapter, pane: "%42")
        out = daemon._h_close_pane(_Ctrl(), {"pane": "somnium:SE"})

    assert out["status"] == "queued"
    assert out["operation"] == "close-pane"
    assert out["reason"] == "typing_guard"
    assert out["gate"]["reason"] == "typing_guard"
    assert out["gate"]["gate"] == "human_lock"
    assert out["gate"]["policy"] == "enqueue"
    assert "queue_handle" in out


def test_h_close_pane_unresolved_enqueues_with_pane_unresolved_reason(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("TMUXCTLD_DEFERRED_SENDS_PATH", str(tmp_path / "deferred-sends.json"))
    monkeypatch.setattr(daemon, "_DEFERRED_SEND_QUEUE", daemon.DeferredSendQueue())
    monkeypatch.setattr(daemon, "_schedule_deferred_drain", lambda _pane: None)
    monkeypatch.setattr(daemon.send_gate, "_pane_human_locked", lambda _phys: False)

    class _Ctrl:
        adapter = object()

        def close_pane(self, pane: str, *, timeout: float = 3.0) -> None:
            raise AssertionError("must not close an unresolved pane")

    def unresolved(adapter: object, pane: str) -> None:
        raise ValueError("no pane for label")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(daemon, "resolve_to_physical", unresolved)
        out = daemon._h_close_pane(_Ctrl(), {"pane": "somnium:SE"})

    assert out["status"] == "queued"
    assert out["operation"] == "close-pane"
    assert out["reason"] == "pane_unresolved"
    assert out["gate"]["reason"] == "pane_unresolved"
    assert out["gate"]["gate"] == "pane_unresolved"
    assert out["gate"]["target"] == "somnium:SE"
    assert out["gate"]["policy"] == "enqueue"
    assert "queue_handle" in out


def test_h_close_pane_cleared_in_place_marks_no_retire_required(monkeypatch) -> None:
    monkeypatch.setattr(daemon.send_gate, "_pane_human_locked", lambda _phys: False)

    class _Ctrl:
        adapter = object()

        def close_pane(self, pane, *, timeout=3.0):
            return {"status": "cleared_in_place", "pane": pane, "pane_class": "slot"}

    out = daemon._h_close_pane(_Ctrl(), {"pane": "%8"})
    assert out["retire_required"] is False
    assert out["close_transaction_complete"] is True


def test_pane_inventory_reports_roster_without_raw_scrape_by_callers() -> None:
    class _Adapter:
        def run(self, *args, allow_failure=False):
            assert args[:3] == ("list-panes", "-a", "-F")
            return "%8\tsomnium:SE\tsomnium\tSE title\t0\ti-1\tw-1\tpersona\toccupied\t123"

    class _Ctrl:
        adapter = _Adapter()

    out = daemon._h_pane_inventory(_Ctrl(), {})
    assert out["ok"] is True
    assert out["panes"][0]["label"] == "somnium:SE"
    assert out["panes"][0]["slot"] == "somnium"
    assert out["panes"][0]["cardinal"] == "SE"
    assert out["panes"][0]["live"] is True


def test_ledger_reconcile_scrubs_chrome_on_unbound_free_slot(monkeypatch):
    """/reconcile must derive chrome from ledger bind state, including FREE slots.

    A free slot with no active wrapper-ledger row, no live agent, and no boot grace
    may still carry stale tint/title from a prior bind. Reconcile is the ledger
    transaction that makes bind state canonical, so it must scrub that chrome in
    the same pass rather than merely returning ok.
    """

    class FreeSlotAdapter:
        def __init__(self):
            self.cleared = []

        def run(self, *args, allow_failure=False):  # noqa: ARG002
            if args[:2] == ("list-panes", "-a"):
                fmt = args[-1]
                if "TOKEN_API_WRAPPER_ID" in fmt:
                    return ""
                return "%22\tsomnium:N\tsomnium\t4242\t0"
            return ""

        def clear_runtime_state(self, target):
            self.cleared.append(target)

    adapter = FreeSlotAdapter()
    control = TmuxControlPlane(adapter=adapter)
    monkeypatch.setattr("tmuxctl.occupancy._active_agent", lambda pane_pid: False)
    monkeypatch.setattr(
        "tmuxctl.wrapper_ledger.LEDGER.reconcile_from_tmux", lambda a: {"open_rows": 0}
    )

    out = control.ledger_reconcile()

    assert out["chrome_scrubbed_unbound_panes"] == ["%22"]
    assert adapter.cleared == ["%22"]


def test_ledger_reconcile_does_not_scrub_ledger_bound_or_singleton_panes(monkeypatch):
    class MixedAdapter:
        def __init__(self):
            self.cleared = []

        def run(self, *args, allow_failure=False):  # noqa: ARG002
            if args[:2] == ("list-panes", "-a"):
                fmt = args[-1]
                if "TOKEN_API_WRAPPER_ID" in fmt:
                    return ""
                return "\n".join(
                    [
                        "%1\tsomnium:N\tsomnium\t100\t0",
                        "%2\tmechanicus:1\tmechanicus\t101\t0",
                        "%3\tcouncil:custodes\tcouncil\t102\t0",
                    ]
                )
            return ""

        def clear_runtime_state(self, target):
            self.cleared.append(target)

    adapter = MixedAdapter()
    control = TmuxControlPlane(adapter=adapter)
    monkeypatch.setattr("tmuxctl.occupancy._active_agent", lambda pane_pid: False)

    class Row:
        def __init__(self, instance_id):
            self.instance_id = instance_id

        def as_dict(self):
            return {"instance_id": self.instance_id}

    def fake_resolve(*, pane_positional_id, **kwargs):  # noqa: ARG001
        if pane_positional_id == "mechanicus:1":
            return Row("i-bound")
        return None

    monkeypatch.setattr("tmuxctl.wrapper_ledger.LEDGER.resolve", fake_resolve)
    monkeypatch.setattr(
        "tmuxctl.wrapper_ledger.LEDGER.reconcile_from_tmux", lambda a: {"open_rows": 1}
    )

    out = control.ledger_reconcile()

    assert out["chrome_scrubbed_unbound_panes"] == ["%1"]
    assert adapter.cleared == ["%1"]


def test_ledger_reconcile_scrubs_unbound_nonfree_hollow_slot(monkeypatch):
    """Unbound chrome drift is scrubbed even before the pane reaches freelist.

    A hollow wrapper row can keep a bare shell out of freelist for one pass. Since
    chrome derives from bind, reconcile scrubs when no live TUI is present.
    """

    class HollowAdapter:
        def __init__(self):
            self.cleared = []

        def run(self, *args, allow_failure=False):  # noqa: ARG002
            if args[:2] == ("list-panes", "-a"):
                fmt = args[-1]
                if "TOKEN_API_WRAPPER_ID" in fmt:
                    return _raw_scan_line(
                        wrapper_id="w-hollow",
                        instance_id="",
                        pane_id="somnium:N",
                        engine="claude",
                        working_dir="/tmp/stale",
                    )
                return "%22\tsomnium:N\tsomnium\t4242\t0"
            return ""

        def clear_runtime_state(self, target):
            self.cleared.append(target)

    adapter = HollowAdapter()
    control = TmuxControlPlane(adapter=adapter)
    monkeypatch.setattr("tmuxctl.occupancy._active_agent", lambda pane_pid: False)

    out = control.ledger_reconcile()

    assert out["chrome_scrubbed_unbound_panes"] == ["%22"]
    assert adapter.cleared == ["%22"]


def test_ledger_reconcile_refuses_to_scrub_unbound_live_tui_divergence(monkeypatch):
    """Live TUI + empty bind is split-brain, not free chrome residue."""

    class LiveTuiAdapter:
        def __init__(self):
            self.cleared = []

        def run(self, *args, allow_failure=False):  # noqa: ARG002
            if args[:2] == ("list-panes", "-a"):
                fmt = args[-1]
                if "TOKEN_API_WRAPPER_ID" in fmt:
                    return ""
                return "%22\tsomnium:N\tsomnium\t4242\t0"
            return ""

        def clear_runtime_state(self, target):
            self.cleared.append(target)

    adapter = LiveTuiAdapter()
    control = TmuxControlPlane(adapter=adapter)
    monkeypatch.setattr("tmuxctl.occupancy._active_agent", lambda pane_pid: True)
    monkeypatch.setattr(
        "tmuxctl.wrapper_ledger.LEDGER.reconcile_from_tmux", lambda a: {"open_rows": 0}
    )

    out = control.ledger_reconcile()

    assert out["chrome_scrubbed_unbound_panes"] == []
    assert adapter.cleared == []
    assert out["chrome_unbound_live_divergences"] == [
        {
            "pane": "%22",
            "pane_label": "somnium:N",
            "reason": "live_agent_without_bind",
        }
    ]
    assert out["chrome_unbound_live_divergence_count"] == 1
