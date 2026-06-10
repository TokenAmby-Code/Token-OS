import importlib.util
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def load_satellite_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "token-satellite.py"
    spec = importlib.util.spec_from_file_location("token_satellite_for_tests", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeLogFollower:
    """Stand-in for DeskflowLogFollower in watchdog tests.

    Settable ``connected`` plus a ``feed(line)`` that runs the REAL classifier on a
    real deskflow-core log string and mirrors ``_emit`` onto the watchdog's edge
    queue — so tests drive state with the exact strings deskflow emits, never a
    paraphrase.
    """

    def __init__(self, module: Any, edge_queue: queue.Queue) -> None:
        self._module = module
        self._edge_queue = edge_queue
        self.connected: bool = False

    def start(self) -> None:
        pass

    def join(self, timeout: float | None = None) -> None:
        pass

    def feed(self, line: str) -> str | None:
        follower = self._module.DeskflowLogFollower
        edge = follower._classify(line)
        if edge is None:
            return None
        if edge == follower.UP:
            self.connected = True
        elif edge == follower.DROP:
            self.connected = False
        self._edge_queue.put(edge)
        return edge


def make_watchdog(
    module: Any,
    *,
    follower_connected: bool = False,
    wait_results: list[bool] | None = None,
    observation: dict[str, Any] | None = None,
    record_recover: bool = False,
) -> Any:
    """Real ``DeskFlowWatchdog`` with leaf primitives replaced by recorders.

    Overrides are set as INSTANCE attributes, so every internal ``self._leaf()``
    call resolves to the recorder at any nesting depth (the wrapper/unbound-method
    pattern only doubles one level deep).
    """
    wd = module.DeskFlowWatchdog()
    actions = []
    wd.actions = actions

    wd._follower = FakeLogFollower(module, wd._edge_queue)
    wd._follower.connected = follower_connected

    if observation is None:
        observation = {
            "deskflow_running": True,
            "deskflow_listening": True,
            "deskflow_connected": follower_connected,
            "mac_reachable": True,
            "mac_client_running": False,
        }
    wd._observe = lambda: dict(observation)
    wd._check_deskflow_connected = lambda: wd._follower.connected
    wd._follower_connected = lambda: wd._follower.connected
    wd._opportunistic_defer = lambda seconds=None: wd._follower.connected

    pending = list(wait_results or [])

    def _wait_for_connection(seconds: float | None = None) -> bool:
        actions.append("wait")
        if pending:
            ok = pending.pop(0)
            if ok:
                wd._mark_connected()
            return ok
        return False

    wd._wait_for_connection = _wait_for_connection

    wd._start_mac_client = lambda: actions.append("mac_quick_reconnect")
    wd._reload_deskflow_server = lambda: actions.append("local_reload")
    wd._stop_deskflow_server = lambda: actions.append("local_stop")

    def _start_deskflow_server() -> bool:
        # Mirror the real method's bool contract (used by the boot path).
        actions.append("local_start")
        return True

    wd._start_deskflow_server = _start_deskflow_server
    wd._reload_mac_client = lambda: actions.append("mac_reload")
    wd._restart_mac_client = lambda: actions.append("mac_full_restart")
    wd._schedule_backoff = lambda: actions.append("backoff")
    wd._invite_mac = lambda: actions.append("invite_mac")

    if record_recover:
        wd._recover_connection = lambda reason: actions.append(("recover", reason))

    return wd


# Real deskflow-core log lines (captured live 2026-06-03), with the
# "[timestamp] LEVEL:" prefix the classifier must see through.
LINE_DROP = '[2026-06-03T16:49:45.999] IPC: client "Tokens-Mac-Mini" has disconnected'
LINE_UP_IPC = '[2026-06-03T16:49:48.508] IPC: client "Tokens-Mac-Mini" has connected'
LINE_UP_NOTE = "[2026-06-03T16:49:48.487] NOTE: accepted client connection"
LINE_SERVER_UP = "[2026-06-03T16:49:10.768] IPC: started server, waiting for clients"
LINE_NOISE = "[2026-06-03T16:50:28.945] ERROR: failed to accept secure socket"


# ── Classifier ──


def test_classify_maps_real_log_lines_to_edges() -> None:
    module = load_satellite_module()
    f = module.DeskflowLogFollower
    assert f._classify(LINE_DROP) == f.DROP
    assert f._classify(LINE_UP_IPC) == f.UP
    assert f._classify(LINE_UP_NOTE) == f.UP
    assert f._classify(LINE_SERVER_UP) == f.SERVER_UP
    assert f._classify(LINE_NOISE) is None
    assert f._classify("") is None


# ── Edge dispatch ──


def test_server_up_invites_mac_exactly_once() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module)  # follower not connected
    module.DeskFlowWatchdog._on_server_up(wd)
    assert wd.actions.count("invite_mac") == 1


def test_boot_server_up_is_suppressed_once() -> None:
    # The eager boot invite arms a one-shot suppression of the paired SERVER_UP.
    module = load_satellite_module()
    wd = make_watchdog(module)
    wd._suppress_boot_server_up = True
    module.DeskFlowWatchdog._on_server_up(wd)
    assert wd.actions == []
    assert wd._suppress_boot_server_up is False
    # A later SERVER_UP (real restart, disconnected) invites normally.
    module.DeskFlowWatchdog._on_server_up(wd)
    assert wd.actions == ["invite_mac"]


def test_server_up_while_connected_does_not_invite() -> None:
    # SERVER_UP must not bounce an already-connected client (invite is stop+start).
    module = load_satellite_module()
    wd = make_watchdog(module, follower_connected=True)
    module.DeskFlowWatchdog._on_server_up(wd)
    assert wd.actions == []


def test_follower_connected_latches_fallback_probe() -> None:
    # When only the one-shot Established probe is positive, the follower's derived
    # state must latch so _observe()/status stop reporting the link down.
    module = load_satellite_module()
    wd = make_watchdog(module)
    wd._follower.connected = False
    wd._check_deskflow_connected = lambda: True
    # Use the REAL _follower_connected (make_watchdog stubs it out by default).
    wd._follower_connected = module.DeskFlowWatchdog._follower_connected.__get__(wd)
    assert wd._follower_connected() is True
    assert wd._follower.connected is True


def test_drop_while_running_schedules_recovery() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module, record_recover=True)
    wd.state = "running"
    module.DeskFlowWatchdog._on_link_down(wd)
    assert wd.state == "waiting"
    assert ("recover", "drop_edge") in wd.actions


def test_drop_while_not_running_does_not_recover() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module, record_recover=True)
    wd.state = "waiting"
    module.DeskFlowWatchdog._on_link_down(wd)
    assert wd.actions == []


def test_up_while_ceased_resurrects_to_running() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module)
    wd.state = "ceased"
    wd._follower.connected = True
    module.DeskFlowWatchdog._on_link_up(wd)
    assert wd.state == "running"


def test_stopped_swallows_edges() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module)
    wd.state = "stopped"
    module.DeskFlowWatchdog._dispatch_edge(wd, module.DeskflowLogFollower.SERVER_UP)
    assert wd.state == "stopped"
    assert wd.actions == []


def test_held_swallows_edges_until_expiry() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module, record_recover=True)
    wd.state = "held"
    wd.hold_until = time.time() + 3600
    module.DeskFlowWatchdog._dispatch_edge(wd, module.DeskflowLogFollower.DROP)
    assert wd.state == "held"
    assert wd.actions == []


# ── Recovery ladder ──


def test_recovery_stops_after_mac_quick_reconnect() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module, wait_results=[True])
    module.DeskFlowWatchdog._recover_connection(wd, "test")
    assert wd.actions == ["mac_quick_reconnect", "wait"]
    assert wd.state == "running"


def test_recovery_stops_after_local_reload_before_full_restart() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module, wait_results=[False, True])
    module.DeskFlowWatchdog._recover_connection(wd, "test")
    assert wd.actions == ["mac_quick_reconnect", "wait", "local_reload", "wait"]
    assert "local_stop" not in wd.actions
    assert "mac_full_restart" not in wd.actions
    assert wd.state == "running"


def test_opportunistic_defer_aborts_ladder_before_touching_mac() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module)
    # The link drops, then self-heals within the grace window (real strings).
    wd._follower.feed(LINE_DROP)
    wd._follower.feed(LINE_UP_IPC)  # connected → True
    module.DeskFlowWatchdog._recover_connection(wd, "drop_edge")
    assert "mac_quick_reconnect" not in wd.actions
    assert wd.actions == []
    assert wd.state == "running"


def test_recovery_lock_skips_overlapping_recovery() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module, wait_results=[])
    assert wd._recovery_lock.acquire(blocking=False)
    try:
        module.DeskFlowWatchdog._recover_connection(wd, "test")
    finally:
        wd._recovery_lock.release()
    assert wd.actions == []


# ── Idle-tick recovery driver (the recovery-wedge fix) ──


def test_edge_loop_timeout_blocks_indefinitely_unless_timed_work() -> None:
    # No periodic liveness poll: settled states block until an edge
    # (or the stop sentinel) arrives.
    module = load_satellite_module()
    wd = make_watchdog(module)
    for state in ("running", "ceased", "stopped", "starting"):
        wd.state = state
        assert wd._edge_loop_timeout() is None


def test_edge_loop_timeout_held_wakes_at_hold_expiry() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module)
    wd.state = "held"
    wd.hold_until = time.time() + 5
    t = wd._edge_loop_timeout()
    assert 1.0 <= t <= 6.0
    # Expired hold clamps to the floor so the resume runs promptly.
    wd.hold_until = time.time() - 10
    assert wd._edge_loop_timeout() == 1.0


def test_idle_tick_resumes_expired_hold() -> None:
    module = load_satellite_module()
    # Connected at expiry → settle straight to running.
    wd = make_watchdog(module, follower_connected=True, record_recover=True)
    wd.state = "held"
    wd.hold_until = time.time() - 1
    module.DeskFlowWatchdog._on_idle_tick(wd)
    assert wd.state == "running"
    # Un-connected at expiry → waiting with an immediate retry armed (a DROP
    # swallowed during the hold produces no further edge).
    wd2 = make_watchdog(module, record_recover=True)
    wd2.state = "held"
    wd2.hold_until = time.time() - 1
    module.DeskFlowWatchdog._on_idle_tick(wd2)
    assert wd2.state == "waiting"
    assert wd2.next_recovery_at <= time.time()


def test_idle_tick_keeps_unexpired_hold() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module, record_recover=True)
    wd.state = "held"
    wd.hold_until = time.time() + 60
    module.DeskFlowWatchdog._on_idle_tick(wd)
    assert wd.state == "held"
    assert not any(isinstance(a, tuple) and a[0] == "recover" for a in wd.actions)


def test_edge_loop_timeout_short_while_waiting_respects_next_recovery_at() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module)
    wd.state = "waiting"
    # A retry scheduled ~5s out yields a short timeout.
    wd.next_recovery_at = time.time() + 5
    t = wd._edge_loop_timeout()
    assert 1.0 <= t <= 6.0
    # Overdue retry clamps to the 1.0s floor, never negative.
    wd.next_recovery_at = time.time() - 100
    assert wd._edge_loop_timeout() == 1.0
    # A far-future retry waits the full remaining delay (no cap — there is no
    # other timed work to wake for).
    wd.next_recovery_at = time.time() + 10_000
    assert wd._edge_loop_timeout() > 9_000


def test_idle_tick_drives_recovery_when_waiting_and_due() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module, record_recover=True)
    wd.state = "waiting"
    wd._follower.connected = False
    wd.next_recovery_at = time.time() - 1  # due
    module.DeskFlowWatchdog._on_idle_tick(wd)
    assert ("recover", "waiting") in wd.actions


def test_idle_tick_drives_recovery_when_backoff_and_due() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module, record_recover=True)
    wd.state = "backoff"
    wd._follower.connected = False
    wd.next_recovery_at = time.time() - 1
    module.DeskFlowWatchdog._on_idle_tick(wd)
    assert ("recover", "backoff") in wd.actions


def test_idle_tick_no_recovery_when_not_due() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module, record_recover=True)
    wd.state = "waiting"
    wd._follower.connected = False
    wd.next_recovery_at = time.time() + 1000  # not due yet
    module.DeskFlowWatchdog._on_idle_tick(wd)
    assert not any(a == ("recover", "waiting") for a in wd.actions)


def test_idle_tick_no_recovery_when_running() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module, record_recover=True)
    wd.state = "running"
    wd.next_recovery_at = time.time() - 1
    module.DeskFlowWatchdog._on_idle_tick(wd)
    assert not any(isinstance(a, tuple) and a[0] == "recover" for a in wd.actions)


def test_idle_tick_no_recovery_when_ceased() -> None:
    # ceased waits for an UP edge to resurrect — the idle tick must not retry.
    module = load_satellite_module()
    wd = make_watchdog(module, record_recover=True)
    wd.state = "ceased"
    wd._follower.connected = False
    wd.next_recovery_at = time.time() - 1
    module.DeskFlowWatchdog._on_idle_tick(wd)
    assert not any(isinstance(a, tuple) and a[0] == "recover" for a in wd.actions)


def test_idle_tick_no_recovery_when_connected() -> None:
    module = load_satellite_module()
    wd = make_watchdog(module, record_recover=True)
    wd.state = "waiting"
    wd._follower.connected = True  # already healed
    wd.next_recovery_at = time.time() - 1
    module.DeskFlowWatchdog._on_idle_tick(wd)
    assert not any(isinstance(a, tuple) and a[0] == "recover" for a in wd.actions)


def test_idle_tick_no_recovery_when_stopped() -> None:
    # force_stop means leave the server down — the idle tick must not restart it.
    module = load_satellite_module()
    wd = make_watchdog(module, record_recover=True)
    wd.state = "stopped"
    wd._follower.connected = False
    wd.next_recovery_at = time.time() - 1
    module.DeskFlowWatchdog._on_idle_tick(wd)
    assert not any(isinstance(a, tuple) and a[0] == "recover" for a in wd.actions)


def test_liveness_poll_is_gone() -> None:
    # The 5-min process-liveness poll was removed by decree (2026-06-10): a KVM
    # drop is immediately user-visible, and deploys (satellite boot) re-check.
    module = load_satellite_module()
    assert not hasattr(module, "DESKFLOW_PROCESS_CHECK_INTERVAL")
    assert not hasattr(module.DeskFlowWatchdog, "_ensure_server_alive")


def test_stop_unblocks_edge_loop_with_sentinel() -> None:
    # In settled states the loop parks on a timeout-less queue.get; stop() must
    # push a sentinel so shutdown never hangs on join().
    module = load_satellite_module()
    wd = make_watchdog(module)
    wd._thread = None
    module.DeskFlowWatchdog.stop(wd)
    assert wd._stop_event.is_set()
    assert wd._edge_queue.get_nowait() is None


def test_edge_dequeued_after_stop_is_not_dispatched() -> None:
    # A real edge queued ahead of the stop sentinel must not trigger invites/
    # recovery once shutdown has started — the loop re-checks the stop event
    # after every wake-up. Simulate stop firing mid-iteration: the timeout hook
    # runs right before the dequeue, so setting stop there lands in the window
    # between the loop-condition check and the dispatch guard.
    module = load_satellite_module()
    wd = make_watchdog(module, follower_connected=True)
    wd._wait_for_tailscale = lambda: True
    dispatched = []
    wd._dispatch_edge = lambda edge: dispatched.append(edge)
    wd._follower.feed(LINE_DROP)  # real edge queued before stop

    def _timeout_then_stop() -> float:
        wd._stop_event.set()
        return 0.05

    wd._edge_loop_timeout = _timeout_then_stop
    module.DeskFlowWatchdog._run(wd)
    assert dispatched == []


# ── Follower tail mechanics ──


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_follower_tails_edges_and_reopens_on_truncation(tmp_path: Any) -> None:
    module = load_satellite_module()
    log = tmp_path / "deskflow-core.log"
    # Seed with a REAL edge line: if the first open replayed history instead of
    # seeking to end, this DROP would be classified and queued — so the assertions
    # below actually exercise the "skip existing history on first open" contract.
    log.write_text(LINE_DROP + "\n")

    original = module.DESKFLOW_CORE_LOG
    module.DESKFLOW_CORE_LOG = log
    edge_queue: queue.Queue = queue.Queue()
    stop = threading.Event()
    follower = module.DeskflowLogFollower(edge_queue, stop)
    try:
        follower.start()
        assert _wait_until(lambda: follower._fh is not None)

        # The seeded DROP must NOT be replayed: nothing queued, connected untouched.
        try:
            stray = edge_queue.get(timeout=0.5)
            raise AssertionError(f"first open replayed history: {stray}")
        except queue.Empty:
            pass
        assert follower.connected is False

        with log.open("a") as fh:
            fh.write(LINE_UP_IPC + "\n")
            fh.flush()
        assert edge_queue.get(timeout=3) == module.DeskflowLogFollower.UP
        assert _wait_until(lambda: follower.connected is True)

        # Truncate + rewrite (logrotate / manual delete-recreate). The reopen must
        # read from the START of the new file, so this DROP is not lost.
        with log.open("w") as fh:
            fh.write(LINE_DROP + "\n")
            fh.flush()
        assert edge_queue.get(timeout=3) == module.DeskflowLogFollower.DROP
        assert _wait_until(lambda: follower.connected is False)
    finally:
        stop.set()
        follower.join(timeout=3)
        module.DESKFLOW_CORE_LOG = original
