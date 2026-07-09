"""Comms-router unification invariants.

The authoritative notify/TTS service is the single comms middleware. Feature
code expresses intent ("notify: message + optional tactile/banner") and the
router (`routes.tts.dispatch_notify` / `resolve_tts_device`) owns device
selection, quiet-hours gating, and fanout. Circumventing the router by sending
spoken text phone-direct via `_send_to_phone(tts_text=...)` is a violation.

These are unit/structural guards, not a substitute for the live-path validation
(GT ready-for-ack, break-exhausted, AskUserQuestion) the migration was
exercised against.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

TOKEN_API_DIR = Path(__file__).resolve().parents[1]

# Modules that ARE the router internals — the only code allowed to reach the
# phone transport directly.
_ROUTER_INTERNALS = {"routes/tts.py", "notify.py", "phone_service.py"}

# Lines bearing this marker are documented, reviewed exceptions (e.g. delivering
# a phone-hosted session's own TTS to its host device — not a geofence-routable
# notification).
_ALLOW_MARKER = "comms-router-allow"


def _load(mod: str):
    if str(TOKEN_API_DIR) not in sys.path:
        sys.path.insert(0, str(TOKEN_API_DIR))
    return importlib.import_module(mod)


def _feature_source_files() -> list[Path]:
    files = [TOKEN_API_DIR / "main.py"]
    files += sorted((TOKEN_API_DIR / "routes").glob("*.py"))
    out: list[Path] = []
    for f in files:
        rel = str(f.relative_to(TOKEN_API_DIR))
        if rel in _ROUTER_INTERNALS:
            continue
        out.append(f)
    return out


# ---------------- Guard: no feature-code TTS phone-bypass ----------------


def test_no_feature_code_sends_tts_text_phone_direct():
    """No feature-code callsite may put a spoken `tts_text` payload onto the
    phone transport. Spoken text must go through `dispatch_notify` so the
    geofence-first router decides the audible device."""
    offenders: dict[str, list[tuple[int, str]]] = {}
    for f in _feature_source_files():
        bad: list[tuple[int, str]] = []
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if '"tts_text"' in line and _ALLOW_MARKER not in line:
                bad.append((i, line.strip()[:100]))
        if bad:
            offenders[str(f.relative_to(TOKEN_API_DIR))] = bad
    assert not offenders, (
        "Feature code must route spoken text through the comms router "
        "(dispatch_notify), not phone-direct via a tts_text payload. "
        f"Offenders: {offenders}"
    )


# ---------------- Endpoint surface ----------------


def test_notify_endpoint_surface(app_env):
    """`/api/notify` is the single authoritative entry. The TTS-only sibling
    `/api/notify/tts` is retired (CLIs repointed to /api/notify)."""
    paths = {getattr(r, "path", None) for r in app_env.main.app.routes}
    for route in app_env.main.app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is None:
            continue
        prefix = getattr(getattr(route, "include_context", None), "prefix", "") or ""
        paths.update(prefix + r.path for r in getattr(original_router, "routes", []))
    assert "/api/notify" in paths
    assert "/api/notify/tts" not in paths


# ---------------- dispatch_notify core ----------------


def _recorders(monkeypatch, tts, *, enqueue_result=None, completion_outcome=None):
    """Record dispatch_notify's router calls.

    dispatch_notify no longer speaks directly — it enqueues via the single gated
    queue (``queue_tts``) and awaits a completion future the worker resolves when
    playback truly finishes. The fake records each enqueue and, when the enqueue
    reports ``queued``, resolves the completion with a truthful
    ``{success, route, audio_delivered}`` outcome so the awaited front door
    unblocks instantly (no 90s ``wait_for`` hang). Tactile/banner still reach the
    phone transport directly, but NEVER a ``tts_text`` payload.
    """
    calls = {"enqueue": [], "phone": []}
    outcome = completion_outcome or {"success": True, "route": "phone", "audio_delivered": True}
    enqueue_ret = enqueue_result or {"success": True, "queued": True}

    async def fake_queue_tts(instance_id, message, queue_target="pause", completion=None, **kwargs):
        calls["enqueue"].append((instance_id, message, queue_target))
        if enqueue_ret.get("queued") and completion is not None and not completion.done():
            completion.set_result(outcome)
        return enqueue_ret

    def fake_send_to_phone(endpoint, params):
        calls["phone"].append((endpoint, dict(params or {})))
        return {"success": True}

    monkeypatch.setattr(tts, "queue_tts", fake_queue_tts)
    monkeypatch.setattr(tts, "_send_to_phone", fake_send_to_phone)
    return calls


def test_dispatch_notify_speaks_via_router_and_never_phone_direct(monkeypatch: Any) -> None:
    tts = _load("routes.tts")
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    calls = _recorders(monkeypatch, tts)

    result = asyncio.run(tts.dispatch_notify("hello world", vibe=30, banner="hi"))

    # Spoken text went through the SINGLE gated queue (queue_tts), once, to hot.
    assert len(calls["enqueue"]) == 1
    iid, msg, queue_target = calls["enqueue"][0]
    assert msg == "hello world"
    assert queue_target == "hot"
    # Instance-less notify rides the synthetic ``system`` sender (not None): a
    # regression to a bare None would still enqueue but break the contract.
    assert iid == tts.SYSTEM_INSTANCE_ID
    # Tactile/banner reached the phone as device-control — but NEVER a tts_text.
    assert len(calls["phone"]) == 1
    _ep, params = calls["phone"][0]
    assert "tts_text" not in params
    assert params.get("vibe") == 30
    assert params.get("banner_text") == "hi"
    assert result.get("delivered") is True


def test_dispatch_notify_tts_failure_is_not_masked_by_tactile(monkeypatch: Any) -> None:
    """For spoken notifications, top-level delivered means true audio playback.

    A successful banner/vibe leg must not recreate the false-success condition
    where /api/notify returns delivered:true while the TTS backend played nothing.
    The line enqueues, but the worker resolves the completion with a dead-backend
    outcome — that truthful failure must surface, not be masked by the tactile leg.
    """
    tts = _load("routes.tts")
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    calls = _recorders(
        monkeypatch,
        tts,
        completion_outcome={"success": False, "route": None, "audio_delivered": False},
    )

    result = asyncio.run(tts.dispatch_notify("hello world", vibe=30, banner="hi"))

    assert len(calls["enqueue"]) == 1
    assert len(calls["phone"]) == 1
    assert result.get("delivered") is False
    assert result.get("audio_delivered") is False
    assert result.get("tactile", {}).get("success") is True


def test_dispatch_notify_not_queued_fails_closed(monkeypatch: Any) -> None:
    """An enqueue that is refused (e.g. instance_not_found) is truthful non-delivery.

    queue_tts can decline to queue (no backend / persona_silent / instance_not_found).
    dispatch_notify must NOT silent-direct-speak around that, and must report
    audio_delivered=False with the refusal reason — never a false success.
    """
    tts = _load("routes.tts")
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    calls = _recorders(
        monkeypatch,
        tts,
        enqueue_result={"success": False, "queued": False, "reason": "instance_not_found"},
    )

    result = asyncio.run(tts.dispatch_notify("hello world", vibe=30))

    assert len(calls["enqueue"]) == 1
    assert result.get("delivered") is False
    assert result.get("audio_delivered") is False
    assert result.get("tts", {}).get("reason") == "instance_not_found"


def test_dispatch_notify_enforcement_bypasses_persona_silent(monkeypatch: Any) -> None:
    """Live enforcement may not disappear behind persona_silent.

    The notify front door must mark the enqueue as enforcement so queue_tts can
    use the system/Custodes voice for a silent persona instead of returning the
    ambiguous /api/notify non-delivery observed in production.
    """
    tts = _load("routes.tts")
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    calls = {"kwargs": []}

    async def fake_queue_tts(instance_id, message, queue_target="pause", completion=None, **kwargs):
        calls["kwargs"].append(kwargs)
        if completion is not None and not completion.done():
            completion.set_result({"success": True, "route": "mac", "audio_delivered": True})
        return {"success": True, "queued": True}

    monkeypatch.setattr(tts, "queue_tts", fake_queue_tts)
    monkeypatch.setattr(tts, "_send_to_phone", lambda *_a, **_k: {"success": True})

    result = asyncio.run(
        tts.dispatch_notify(
            "enforcement line", instance_id="silent-instance", context={"kind": "enforcement"}
        )
    )

    assert result["delivered"] is True
    assert calls["kwargs"][0]["bypass_persona_silent"] is True


def test_dispatch_notify_tactile_only_does_not_speak(monkeypatch: Any) -> None:
    tts = _load("routes.tts")
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    calls = _recorders(monkeypatch, tts)

    asyncio.run(tts.dispatch_notify("", tts=False, vibe=30, banner="blocked"))

    assert calls["enqueue"] == []
    assert len(calls["phone"]) == 1
    _ep, params = calls["phone"][0]
    assert "tts_text" not in params
    assert params.get("banner_text") == "blocked"


def test_dispatch_notify_suppressed_in_quiet_hours(monkeypatch: Any) -> None:
    tts = _load("routes.tts")
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: True)
    calls = _recorders(monkeypatch, tts)

    result = asyncio.run(tts.dispatch_notify("hi", vibe=30, banner="hi"))

    assert result.get("suppressed") is True
    assert calls["enqueue"] == []
    assert calls["phone"] == []


def test_phone_direct_tts_only_occurs_inside_the_router(monkeypatch):
    """The one legitimate phone-direct TTS leg lives INSIDE speak_tts (the
    router), reached only when resolve_tts_device selects the phone."""
    tts = _load("routes.tts")
    sent = []

    # The phone leg now BLOCKS on the buffer_drained callback (real audio-finish)
    # up to PHONE_PLAYBACK_WATCHDOG_S. No callback fires in this structural test, so
    # shrink the watchdog to keep it fast. A missed callback is a delivery failure:
    # Token-OS may not claim audible success without the backend ack.
    monkeypatch.setattr(tts, "PHONE_PLAYBACK_WATCHDOG_S", 0.05)

    def fake_send_to_phone(endpoint, params):
        sent.append((endpoint, dict(params or {})))
        return {"success": True}

    monkeypatch.setattr(tts, "_send_to_phone", fake_send_to_phone)
    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda **kw: {"device": "phone", "reason": "geofence: gym", "discord_bot": None},
    )

    result = tts.speak_tts("away from home")

    assert any(p.get("current_chunk") == "away from home" for _e, p in sent)
    assert result.get("success") is False
    assert result.get("method") == "phone"
    assert result.get("route") is None
    assert result.get("reason") == "phone_playback_unconfirmed"


def test_phone_tts_targets_chunk_endpoint_with_playback_id(monkeypatch: Any) -> None:
    """The phone utterance MUST go to the device's ``/tts-chunk`` executor, not ``/notify``.

    The phone exposes a single keystone TTS atom at
    ``GET /tts-chunk?current_chunk=…&next_chunk=…&playback_id=…`` (m_waitToFinish:true) that fast-acks,
    speaks locally, then POSTs ``/api/tts/chunk-event`` with ``buffer_drained`` at true speech end.
    The phone exposes NO ``/notify`` HTTP macro, so a ``/notify`` send never
    triggers speech and every line would silently fall to the watchdog. This guard
    pins the endpoint + the per-utterance opaque ``playback_id`` so the serialization
    handshake can never be re-pointed at a dead path.
    """
    tts = _load("routes.tts")
    sent = []

    monkeypatch.setattr(tts, "PHONE_PLAYBACK_WATCHDOG_S", 0.05)

    def fake_send_to_phone(endpoint, params):
        sent.append((endpoint, dict(params or {})))
        return {"success": True}

    monkeypatch.setattr(tts, "_send_to_phone", fake_send_to_phone)
    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda **kw: {"device": "phone", "reason": "geofence: gym", "discord_bot": None},
    )

    result = tts.speak_tts("contract line")

    chunk_calls = [(e, p) for e, p in sent if e == "/tts-chunk"]
    assert chunk_calls, f"expected a /tts-chunk send, got endpoints {[e for e, _ in sent]}"
    assert not any(e == "/notify" for e, _ in sent), "TTS must not use the dead /notify path"
    assert not any(e == "/speak" for e, _ in sent), "TTS must not use the retired /speak path"
    endpoint, params = chunk_calls[0]
    assert params.get("current_chunk") == "contract line"
    pid = params.get("playback_id")
    assert isinstance(pid, str) and pid, "a per-utterance opaque playback_id is required"
    assert result.get("success") is False
    assert result.get("route") is None
    assert result.get("reason") == "phone_playback_unconfirmed"


def test_discord_fallthrough_respects_geofence_phone_only(monkeypatch: Any) -> None:
    """If Discord VC fails while geofenced away, fallback is phone-only.

    Discord is intentionally checked before the geofence, but a failed Discord
    leg must not leak away-from-home speech to local WSL/Mac speakers.
    """
    tts = _load("routes.tts")
    sent = []

    # Phone leg blocks on the buffer_drained callback; shrink the watchdog so a
    # missed callback in this structural test advances fast (see note above).
    monkeypatch.setattr(tts, "PHONE_PLAYBACK_WATCHDOG_S", 0.05)
    monkeypatch.setitem(tts.DESKTOP_STATE, "location_zone", "gym")
    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda **kw: {
            "device": "discord",
            "reason": "operator in voice channel",
            "discord_bot": "token-bot",
        },
    )
    monkeypatch.setattr(
        tts,
        "speak_tts_discord",
        lambda *a, **k: {
            "success": False,
            "error": "discord_voice_not_played",
            "reason": "bot_not_in_channel",
        },
    )
    monkeypatch.setattr(tts, "_phone_tts_available", lambda: True)

    def fake_send_to_phone(endpoint, params):
        sent.append((endpoint, dict(params or {})))
        return {"success": True}

    monkeypatch.setattr(tts, "_send_to_phone", fake_send_to_phone)
    monkeypatch.setattr(
        tts,
        "is_satellite_tts_available",
        lambda: (_ for _ in ()).throw(AssertionError("WSL fallback bypassed geofence")),
    )
    monkeypatch.setattr(
        tts,
        "speak_tts_mac",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Mac fallback bypassed geofence")),
    )
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)

    result = tts.speak_tts("away discord fallback")

    assert result.get("success") is False
    assert result.get("route") is None
    assert sent == []


def test_wsl_route_attempts_wsl_not_phone_or_mac_false_success(monkeypatch: Any) -> None:
    """A WSL route is first-class but must not silently fall to phone or Mac."""
    tts = _load("routes.tts")
    sent = []

    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda **kw: {"device": "wsl", "reason": "satellite healthy", "discord_bot": None},
    )

    def fake_send_to_phone(endpoint, params):
        sent.append((endpoint, dict(params or {})))
        return {"success": True}

    monkeypatch.setattr(tts, "_send_to_phone", fake_send_to_phone)
    monkeypatch.setattr(tts, "_phone_tts_available", lambda: True)
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)

    def fake_post(*args, **kwargs):
        raise tts.requests.ConnectionError("offline")

    monkeypatch.setattr(tts.requests, "post", fake_post)

    result = tts.speak_tts("wsl fallback")

    assert result.get("success") is False
    assert result.get("requested_device") == "wsl"
    assert result.get("method") == "wsl_sapi_chunk"
    assert result.get("route") is None
    assert result.get("reason") == "satellite_unreachable"
    assert sent == []


# ---------------- Phone playback-complete (real serialization signal) ----------------


def test_playback_complete_sets_waiting_event() -> None:
    """The phone callback sets the Event the worker thread blocks on.

    This is the whole phone-leg serialization mechanism: ``_send_phone_tts`` parks
    on a ``threading.Event`` keyed by ``playback_id`` until the device POSTs
    /api/tts/playback-complete with that id. The endpoint must set exactly that
    event and report a match.
    """
    tts = _load("routes.tts")
    event = threading.Event()
    tts.pending_phone_playbacks["pid-abc"] = event
    try:
        res = asyncio.run(
            tts.tts_playback_complete(tts.PlaybackCompleteRequest(playback_id="pid-abc"))
        )
        assert res.get("matched") is True
        assert event.is_set()
    finally:
        tts.pending_phone_playbacks.pop("pid-abc", None)


def test_playback_complete_unknown_id_is_tolerated_and_warned(caplog: Any) -> None:
    """An unknown/expired/duplicate id returns 200 + a warning — never errors the
    phone, never silently swallows ([[no-suppress-debounce]])."""
    tts = _load("routes.tts")
    with caplog.at_level(logging.WARNING):
        res = asyncio.run(
            tts.tts_playback_complete(tts.PlaybackCompleteRequest(playback_id="ghost"))
        )
    assert res.get("success") is True
    assert res.get("matched") is False
    assert any("unknown/expired" in rec.getMessage() for rec in caplog.records)


def test_phone_watchdog_advances_on_missed_callback(monkeypatch: Any, caplog: Any) -> None:
    """A missed playback-complete callback must not wedge the worker: the watchdog
    fires, logs, and advances with playback_confirmed=False (a delivery failure)."""
    tts = _load("routes.tts")
    monkeypatch.setattr(tts, "PHONE_PLAYBACK_WATCHDOG_S", 0.05)
    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda **kw: {"device": "phone", "reason": "geofence: gym", "discord_bot": None},
    )
    sent = []

    def fake_send_to_phone(endpoint, params):
        sent.append((endpoint, dict(params or {})))
        return {"success": True}

    monkeypatch.setattr(tts, "_send_to_phone", fake_send_to_phone)

    with caplog.at_level(logging.WARNING):
        result = tts.speak_tts("missed callback line")

    assert result.get("success") is False
    assert result.get("route") is None
    assert result.get("reason") == "phone_playback_unconfirmed"
    assert result.get("playback_confirmed") is False
    # The phone was handed a playback_id to echo back.
    assert any(p.get("playback_id") for _e, p in sent)
    assert any("delivery unconfirmed" in rec.getMessage() for rec in caplog.records)
    # The waiter id is always cleaned up (finally pop).
    assert tts.pending_phone_playbacks == {}


def test_phone_playback_confirmed_when_buffer_drained_arrives(monkeypatch: Any) -> None:
    """When the device POSTs buffer_drained, the blocked worker advances with
    playback_confirmed=True — real audio-finish drives serialization, no estimate."""
    tts = _load("routes.tts")
    # Generous watchdog: the callback (not the watchdog) must be what releases it.
    monkeypatch.setattr(tts, "PHONE_PLAYBACK_WATCHDOG_S", 5)
    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda **kw: {"device": "phone", "reason": "geofence: gym", "discord_bot": None},
    )
    sent_ids: list[str] = []

    def fake_send_to_phone(endpoint, params):
        if params and params.get("playback_id"):
            sent_ids.append(params["playback_id"])
        return {"success": True}

    monkeypatch.setattr(tts, "_send_to_phone", fake_send_to_phone)

    holder: dict[str, Any] = {}

    def run():
        holder["result"] = tts.speak_tts("confirmed line")

    worker = threading.Thread(target=run)
    worker.start()
    try:
        # Wait until the worker thread has registered its playback_id and is blocking.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if sent_ids and sent_ids[0] in tts.pending_phone_playbacks:
                break
            time.sleep(0.01)
        assert sent_ids, "phone never received a playback_id"
        pid = sent_ids[0]

        asyncio.run(
            tts.api_tts_chunk_event(
                tts.TTSChunkEventRequest(event="buffer_drained", backend="phone", playback_id=pid)
            )
        )
        worker.join(timeout=5)

        assert worker.is_alive() is False
        assert holder["result"].get("success") is True
        assert holder["result"].get("route") == "phone"
        assert holder["result"].get("playback_confirmed") is True
    finally:
        # Never leak the non-daemon worker if an assertion above fails while it is
        # still parked on the watchdog: release any pending waiter and join.
        for event in list(tts.pending_phone_playbacks.values()):
            event.set()
        worker.join(timeout=5)


# ---------------- Router consolidation: notify.py delegates ----------------


def test_dispatch_notification_delegates_to_router(monkeypatch):
    """notify.py is no longer a second router — it delegates to the single
    routing brain (dispatch_notify), gaining Discord + geofence parity."""
    notify = _load("notify")
    tts = _load("routes.tts")
    seen = {}

    async def fake_dispatch_notify(message, **kw):
        seen["message"] = message
        seen["kw"] = kw
        return {"delivered": True}

    monkeypatch.setattr(tts, "dispatch_notify", fake_dispatch_notify)

    res = asyncio.run(
        notify.dispatch_notification(notify.NotifyRequest(message="ping", type="tts"))
    )
    assert seen["message"] == "ping"
    assert res.get("delivered") is True


def test_notify_has_no_second_device_router():
    """The parallel WSL>Mac>phone device order is retired; one routing decision."""
    notify = _load("notify")
    assert not hasattr(notify, "DEFAULT_DEVICE_ORDER")
    assert not hasattr(notify, "_select_devices")


# ---------------- force_device / distraction_source dropped ----------------


def test_enforce_request_drops_device_overrides():
    enforce = _load("enforce")
    fields = enforce.EnforceRequest.model_fields
    assert "distraction_source" not in fields
    assert "force_device" not in fields


def test_notify_request_drops_device_overrides():
    notify = _load("notify")
    fields = notify.NotifyRequest.model_fields
    assert "distraction_source" not in fields
    assert "force_device" not in fields
