#!/usr/bin/env python3
"""Regression validation for hook-echo target resolution + landing reconciliation.

Standalone assert harness (NOT pytest — pytest was excised repo-wide by Emperor
ruling #703). Run directly:

    cd tmuxctld && uv run python validate_hook_echo.py

Covers the dispatch-comms-failure-ledger repro classes:

1. Echo target must carry the SEND's resolution (talk/brief resolve labels to
   raw %NN before sending; the old echo re-derived from the raw id and failed
   open to ``target=unresolved`` — 100% repro on Custodes→FG briefs).
2. An ack whose ACTUAL landing pane differs from the send target must never
   echo ``delivered=1 turn=submitted`` (talk ``112885b1`` false-delivery class:
   Pax confirmed non-receipt while the echo claimed delivery).
3. Send-time verify (``wait``) must reject pane-mismatched acks and must
   accept a true-target-pane ack even when the ledger instance id disagrees
   (resolve split-brain healing).
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from tmuxctl import daemon  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    marker = "PASS" if condition else "FAIL"
    print(f"[{marker}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text_then_submit(self, pane, text, **_kwargs):
        self.sent.append((pane, text))


class FakeControl:
    def __init__(self, public_map: dict[str, str] | None = None) -> None:
        self.adapter = FakeAdapter()
        self._map = public_map or {}

    def public_pane_id(self, target: str) -> str:
        if target in self._map:
            return self._map[target]
        raise ValueError(f"no public id for {target}")


def new_sniffer(tmpdir: str) -> daemon.PromptSubmitSniffer:
    return daemon.PromptSubmitSniffer(callbacks_path=Path(tmpdir) / "callbacks.json")


def event(instance_id: str = "", pane: str = "", prompt_hash: str = "") -> dict:
    return {
        "event": "UserPromptSubmit",
        "instance_id": instance_id,
        "pane": pane,
        "prompt_hash": prompt_hash,
        "at": time.monotonic(),
    }


def register(sniffer: daemon.PromptSubmitSniffer, **overrides) -> dict:
    payload = {
        "correlation_id": "talk:112885b1",
        "caller_pane": "%9",
        "target_pane": "%12",
        "target_label": "%12",
        "target_public": "council:pax",
        "instance_id": "inst-pax",
        "payload_hash": "hash-1",
        "since": time.monotonic() - 5,
    }
    payload.update(overrides)
    return sniffer.register_callback(**payload)


def emit(control: FakeControl, callback: dict, ev: dict) -> dict:
    # Bypass live tmux gates: the harness validates echo composition, not the
    # typing-guard chokepoint.
    original_gate = daemon.send_gate.evaluate
    original_defer = daemon._defer_or_drop_typing_guard
    daemon.send_gate.evaluate = lambda argv: None
    daemon._defer_or_drop_typing_guard = lambda **kwargs: None
    try:
        return daemon._emit_prompt_submit_callback(control, callback, ev)
    finally:
        daemon.send_gate.evaluate = original_gate
        daemon._defer_or_drop_typing_guard = original_defer


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Echo target carries the send's resolution instead of re-deriving.
        sniffer = new_sniffer(tmpdir)
        register(sniffer)
        fired = sniffer.pop_matching_callbacks(event(instance_id="inst-pax", pane="%12"))
        check("confirmed match fires exactly one callback", len(fired) == 1)
        control = FakeControl()
        result = emit(control, fired[0], event(instance_id="inst-pax", pane="%12"))
        echo_text = control.adapter.sent[0][1] if control.adapter.sent else ""
        check(
            "echo target uses carried send resolution (not unresolved)",
            "target=council:pax" in echo_text,
            echo_text,
        )
        check(
            "confirmed landing keeps delivered=1 turn=submitted",
            "delivered=1 turn=submitted" in echo_text and "landing=confirmed" in echo_text,
            echo_text,
        )
        check("emit result carries landing verdict", result.get("landing") == "confirmed")

        # 2. Carried resolution missing -> reverse-resolve the physical pane the
        #    send actually hit; raw %NN never leaks into the echo.
        sniffer = new_sniffer(tmpdir)
        register(sniffer, correlation_id="brief:de38931c", target_public="")
        fired = sniffer.pop_matching_callbacks(event(instance_id="inst-pax", pane="%12"))
        control = FakeControl(public_map={"%12": "mechanicus:fabricator-general"})
        emit(control, fired[0], event(instance_id="inst-pax", pane="%12"))
        echo_text = control.adapter.sent[0][1]
        check(
            "echo falls back to reverse-resolving the landing pane",
            "target=mechanicus:fabricator-general" in echo_text,
            echo_text,
        )

        # 3. False-delivery kill: ack from a DIFFERENT pane than the send target
        #    must echo an explicit landing mismatch, never delivered=1.
        sniffer = new_sniffer(tmpdir)
        register(sniffer, correlation_id="talk:misdelivered")
        mismatch_event = event(instance_id="inst-pax", pane="%30")
        fired = sniffer.pop_matching_callbacks(mismatch_event)
        check("pane-mismatched ack still consumes the callback (no silent rot)", len(fired) == 1)
        check(
            "mismatched ack is classified landing=mismatch",
            fired and fired[0].get("landing") == "mismatch",
        )
        control = FakeControl(public_map={"%30": "council:malcador"})
        emit(control, fired[0], mismatch_event)
        echo_text = control.adapter.sent[0][1]
        check(
            "mismatch echo reports delivered=0 turn=landing_mismatch",
            "delivered=0 turn=landing_mismatch" in echo_text,
            echo_text,
        )
        check(
            "mismatch echo names the actual landing pane",
            "landing=council:malcador" in echo_text,
            echo_text,
        )
        check(
            "mismatch echo never claims delivered=1",
            "delivered=1" not in echo_text,
            echo_text,
        )

        # 4. Alias/instance resolution failed at send time (empty instance_id):
        #    an event with no pane and no instance must NOT fire the callback.
        sniffer = new_sniffer(tmpdir)
        register(sniffer, correlation_id="talk:unattributable", instance_id="", target_pane="%12")
        fired = sniffer.pop_matching_callbacks(event(prompt_hash="hash-1"))
        check("unattributable event (no pane, no instance) never fires", len(fired) == 0)

        # 5. Hash disagreement still disqualifies.
        sniffer = new_sniffer(tmpdir)
        register(sniffer, correlation_id="talk:hash-mismatch")
        fired = sniffer.pop_matching_callbacks(
            event(instance_id="inst-pax", pane="%12", prompt_hash="other-hash")
        )
        check("payload-hash mismatch never fires", len(fired) == 0)

        # 6. Send-time wait() rejects a pane-mismatched ack (no false
        #    turn=submitted in the level-1 response).
        sniffer = new_sniffer(tmpdir)
        sniffer.record({"instance_id": "inst-pax", "pane": "%30", "prompt_hash": "hash-1"})
        ack = sniffer.wait(
            instance_id="inst-pax", payload_hash="hash-1", since=0, timeout=0.05, pane="%12"
        )
        check("wait() rejects ack whose landing pane differs from target", ack is None)

        # 7. Send-time wait() accepts a true-target-pane ack even when the
        #    ledger instance id disagrees (resolve split-brain healing).
        sniffer = new_sniffer(tmpdir)
        sniffer.record({"instance_id": "inst-other", "pane": "%12", "prompt_hash": "hash-1"})
        ack = sniffer.wait(
            instance_id="inst-pax", payload_hash="hash-1", since=0, timeout=0.05, pane="%12"
        )
        check(
            "wait() accepts target-pane ack despite instance-id disagreement",
            bool(ack) and ack.get("landing") == "confirmed",
        )

        # 8. Instance-only ack (hook omitted pane) stays a match but is marked
        #    unverified, never silently upgraded to confirmed.
        sniffer = new_sniffer(tmpdir)
        register(sniffer, correlation_id="talk:paneless", target_pane="")
        fired = sniffer.pop_matching_callbacks(event(instance_id="inst-pax"))
        check(
            "paneless ack fires with landing=unverified",
            len(fired) == 1 and fired[0].get("landing") == "unverified",
        )

        # 9. Persistence round-trips the carried target resolution.
        sniffer = new_sniffer(tmpdir)
        register(sniffer, correlation_id="talk:persisted")
        reloaded = new_sniffer(tmpdir)
        reloaded.load_callbacks(force=True)
        fired = reloaded.pop_matching_callbacks(event(instance_id="inst-pax", pane="%12"))
        check(
            "target_public survives persistence across daemon restart",
            len(fired) == 1 and fired[0].get("target_public") == "council:pax",
        )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
