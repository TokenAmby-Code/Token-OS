#!/usr/bin/env python3
"""Behavioral pins for the 2026-07-19 transport-stack failures.

Three defects from one live evening, pinned here:

1. ``send_text_then_submit`` injected payloads into panes sitting in tmux
   copy-mode or a shell reverse-i-search overlay — the text replayed through
   mode bindings / landed in the search box and was never submitted. The
   adapter now runs ``_preflight_clear_pane_overlays`` (mode cancel + overlay
   Escape) before the first payload byte, as a no-op on healthy panes.

2. ``_classify_submit_delivery`` reported ``delivered`` for sends whose text
   landed in a history-search overlay. It now returns a ``failed`` verdict for
   the overlay signature.

3. The deferred-send drain worker exited after a single re-blocked pass, so a
   busy agent pane's queue starved forever (36 sends trapped overnight). The
   worker now persists until the pane's queue is empty and surfaces bounded-age
   starvation on agent-owned panes via the notify path.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tmuxctld" / "lib"))

from tmuxctl import daemon  # noqa: E402
from tmuxctl.tmux_adapter import TmuxAdapter, TmuxSendGated  # noqa: E402


def _wait_until(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# 1. pane-mode / overlay preflight (tmux calls mocked)
# ---------------------------------------------------------------------------


def _instrumented_adapter(monkeypatch, *, in_mode: str, tail: str):
    adapter = TmuxAdapter("tmux")
    calls: list[tuple] = []

    def fake_run(*args, allow_failure=False):
        calls.append(args)
        if args[0] == "display-message":
            return f"{in_mode}\n"
        return ""

    monkeypatch.setattr(adapter, "run", fake_run)
    monkeypatch.setattr(adapter, "capture_pane", lambda pane, lines=10: tail)
    monkeypatch.setattr(
        adapter,
        "send_keys",
        lambda target, *keys, allow_failure=False: calls.append(("send_keys", target, *keys)),
    )
    return adapter, calls


def test_preflight_cancels_copy_mode(monkeypatch):
    adapter, calls = _instrumented_adapter(monkeypatch, in_mode="1", tail="$ ")
    adapter._preflight_clear_pane_overlays("%7")
    assert ("send-keys", "-t", "%7", "-X", "cancel") in calls
    assert not any(call[0] == "send_keys" for call in calls)


def test_preflight_escapes_history_search_overlay(monkeypatch):
    tail = "some scrollback\nbck-i-search: brief: worker report_"
    adapter, calls = _instrumented_adapter(monkeypatch, in_mode="0", tail=tail)
    adapter._preflight_clear_pane_overlays("%7")
    assert ("send_keys", "%7", "Escape") in calls
    assert ("send-keys", "-t", "%7", "-X", "cancel") not in calls


def test_preflight_is_noop_on_healthy_pane(monkeypatch):
    adapter, calls = _instrumented_adapter(monkeypatch, in_mode="0", tail="> \n")
    adapter._preflight_clear_pane_overlays("%7")
    # Probes only: no send-keys writes of any kind reached the pane.
    assert not any(call[0] == "send_keys" for call in calls)
    assert not any(call[0] == "send-keys" for call in calls)


def test_send_text_then_submit_clears_overlay_before_payload(monkeypatch):
    tail = "(reverse-i-search)`': "
    adapter, calls = _instrumented_adapter(monkeypatch, in_mode="0", tail=tail)
    monkeypatch.setattr(adapter, "_preflight_send_text_transaction", lambda target, payload: None)
    adapter.send_text_then_submit("%7", "hello world", submit_settle_seconds=0)
    escape_idx = calls.index(("send_keys", "%7", "Escape"))
    payload_idx = calls.index(("send-keys", "-t", "%7", "-l", "hello world"))
    assert escape_idx < payload_idx


# ---------------------------------------------------------------------------
# 2. delivery classification: search overlay -> failed
# ---------------------------------------------------------------------------


def test_detect_search_overlay_capture_zsh_and_bash():
    payload = "brief: worker report for custodes"
    zsh = "transcript line\nbck-i-search: brief: worker report for custodes_"
    bash = "transcript line\n(reverse-i-search)`brief: worker rep': old command"
    assert daemon._detect_search_overlay_capture(zsh, payload)
    assert daemon._detect_search_overlay_capture(bash, payload)


def test_detect_search_overlay_requires_marker_and_payload():
    payload = "brief: worker report for custodes"
    # Payload delivered normally (no overlay marker): not an overlay failure.
    assert not daemon._detect_search_overlay_capture(f"> {payload}\n", payload)
    # Overlay marker without this payload in its search box: not this send.
    assert not daemon._detect_search_overlay_capture("bck-i-search: something else", payload)
    assert not daemon._detect_search_overlay_capture("", payload)
    assert not daemon._detect_search_overlay_capture("bck-i-search: x", "")


def test_classify_submit_delivery_search_overlay_is_failed(monkeypatch):
    payload = "brief: worker report for custodes"
    monkeypatch.setattr(
        daemon,
        "_capture_pane_text",
        lambda control, phys_pane, lines=20: f"bck-i-search: {payload}",
    )
    monkeypatch.setattr(
        daemon,
        "resolve_agent_for_pane",
        lambda adapter, pane, agent, default="auto": "claude",
    )
    delivery, advisory, _excerpt, verified_by = daemon._classify_submit_delivery(
        SimpleNamespace(adapter=None), phys_pane="%28", text=payload, ack=None
    )
    assert delivery == "failed"
    assert "search" in advisory
    assert verified_by is None


def test_classify_submit_delivery_ack_still_confirmed(monkeypatch):
    monkeypatch.setattr(
        daemon,
        "_capture_pane_text",
        lambda control, phys_pane, lines=20: "bck-i-search: payload",
    )
    delivery, _advisory, _excerpt, verified_by = daemon._classify_submit_delivery(
        SimpleNamespace(adapter=None),
        phys_pane="%28",
        text="payload",
        ack={"event": "UserPromptSubmit"},
    )
    assert delivery == "confirmed"
    assert verified_by == "UserPromptSubmit"


# ---------------------------------------------------------------------------
# 3. deferred-send drain: no starvation
# ---------------------------------------------------------------------------


def _hermetic_drain(monkeypatch, tmp_path, pane: str):
    """Route the drain machinery at a temp queue and stub tmux side effects."""
    queue = daemon.DeferredSendQueue(tmp_path / "deferred.json")
    monkeypatch.setattr(daemon, "_DEFERRED_SEND_QUEUE", queue)
    monkeypatch.setattr(daemon, "TmuxAdapter", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(
        daemon, "TmuxControlPlane", lambda adapter: SimpleNamespace(adapter=adapter)
    )
    monkeypatch.setattr(daemon.send_gate, "_typing_delay_sleep", lambda target: 0.01)
    monkeypatch.setattr(daemon.send_gate, "_QUIET_DELAY_RECHECK_SECONDS", 0.01)
    return queue


def test_drain_worker_persists_after_reblock(monkeypatch, tmp_path):
    """A re-blocked pass must retry, not exit with a non-empty queue.

    Pre-fix behavior: the first TmuxSendGated re-block requeued the item and
    the worker exited permanently; nothing drained the queue again until the
    NEXT enqueue for the pane (the 36-send overnight starvation).
    """
    pane = "%91"
    queue = _hermetic_drain(monkeypatch, tmp_path, pane)
    monkeypatch.setattr(daemon.send_gate, "_pane_human_locked", lambda target: False)
    attempts: list[str] = []

    def handler(control, params):
        attempts.append(str(params.get("_typing_guard_deferred_id")))
        if len(attempts) == 1:
            raise TmuxSendGated({"suppressed": True, "reason": "typing_guard"})
        return {"status": "delivered"}

    monkeypatch.setitem(daemon._DEFERRED_ROUTE_HANDLERS, "/test-route", handler)
    queue.enqueue(
        route="/test-route", params={"pane": pane, "text": "hi"}, pane=pane, phys_pane=pane, gate={}
    )
    daemon._schedule_deferred_drain(pane)
    # Wait for the worker's true completion signal (exited AND queue empty):
    # `queue.size() == 0` alone races the pop->requeue in-flight window, where
    # the item is out of the queue but attempt 2 has not happened yet. Under
    # the pre-fix bug the worker exits holding a non-empty queue, so this
    # condition never becomes true and the pin still fails.
    assert _wait_until(lambda: pane not in daemon._DEFERRED_DRAINING and queue.size() == 0), (
        "worker exited without draining the re-blocked queue"
    )
    assert len(attempts) == 2


def test_drain_starvation_escalates_on_agent_owned_pane(monkeypatch, tmp_path):
    pane = "%92"
    queue = _hermetic_drain(monkeypatch, tmp_path, pane)
    lock = {"locked": True}
    monkeypatch.setattr(daemon.send_gate, "_pane_human_locked", lambda target: lock["locked"])
    monkeypatch.setattr(daemon, "_pane_agent_owned", lambda target: True)
    monkeypatch.setattr(daemon, "_safe_public_role", lambda target: "council:custodes")
    notified: list[dict] = []
    monkeypatch.setattr(
        daemon, "_notify_deferred_starvation", lambda **kwargs: notified.append(kwargs)
    )
    monkeypatch.setitem(
        daemon._DEFERRED_ROUTE_HANDLERS, "/test-route", lambda control, params: {"status": "ok"}
    )
    queue.enqueue(
        route="/test-route", params={"pane": pane, "text": "hi"}, pane=pane, phys_pane=pane, gate={}
    )
    with queue._lock:  # age the item past the starvation bound
        queue._items[0]["queued_at"] = time.time() - 3600.0

    daemon._schedule_deferred_drain(pane)
    assert _wait_until(lambda: bool(notified)), "starvation was never surfaced"
    assert notified[0]["queued"] == 1
    assert notified[0]["age_seconds"] >= 3600.0 - 5
    # Delivery still respects the live lock: nothing drained yet.
    assert queue.size() == 1
    lock["locked"] = False
    assert _wait_until(lambda: queue.size() == 0), "queue not drained after guard cleared"
    assert len(notified) == 1, "starvation notify must fire once per episode"
    assert _wait_until(lambda: pane not in daemon._DEFERRED_DRAINING)


def test_drain_starvation_does_not_escalate_human_pane(monkeypatch, tmp_path):
    pane = "%93"
    queue = _hermetic_drain(monkeypatch, tmp_path, pane)
    lock = {"locked": True}
    monkeypatch.setattr(daemon.send_gate, "_pane_human_locked", lambda target: lock["locked"])
    monkeypatch.setattr(daemon, "_pane_agent_owned", lambda target: False)
    notified: list[dict] = []
    monkeypatch.setattr(
        daemon, "_notify_deferred_starvation", lambda **kwargs: notified.append(kwargs)
    )
    monkeypatch.setitem(
        daemon._DEFERRED_ROUTE_HANDLERS, "/test-route", lambda control, params: {"status": "ok"}
    )
    queue.enqueue(
        route="/test-route", params={"pane": pane, "text": "hi"}, pane=pane, phys_pane=pane, gate={}
    )
    with queue._lock:
        queue._items[0]["queued_at"] = time.time() - 3600.0

    daemon._schedule_deferred_drain(pane)
    time.sleep(0.3)
    assert not notified, "human-attended pane must not trigger agent-starvation escalation"
    assert queue.size() == 1
    lock["locked"] = False
    assert _wait_until(lambda: queue.size() == 0)
    assert _wait_until(lambda: pane not in daemon._DEFERRED_DRAINING)


def test_oldest_queued_at_for_pane(tmp_path):
    queue = daemon.DeferredSendQueue(tmp_path / "deferred.json")
    first = queue.enqueue(route="/r", params={}, pane="%5", phys_pane="%5", gate={})
    queue.enqueue(route="/r", params={}, pane="%5", phys_pane="%5", gate={})
    queue.enqueue(route="/r", params={}, pane="%6", phys_pane="%6", gate={})
    assert queue.oldest_queued_at_for_pane("%5") == first["queued_at"]
    assert queue.oldest_queued_at_for_pane("%404") is None
