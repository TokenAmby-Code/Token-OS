"""Phone and Pavlok transport helpers.

Extracted from main.py so route modules can import notification primitives
without reaching back through the _main() lazy import.
"""

import asyncio
import logging
import threading
import time
from datetime import datetime

import requests

from shared import (
    DB_PATH,
    DESKTOP_STATE,
    PAVLOK_CONFIG,
    PAVLOK_STATE,
    PHONE_CONFIG,
    PHONE_STATE,
    TTS_GLOBAL_MODE,
    log_event,
)

logger = logging.getLogger("token_api")

TWITTER_ZAP_COOLDOWN_FILE = DB_PATH.parent / "twitter_zap_cooldown.txt"
TWITTER_ZAP_COOLDOWN_SECS = 1800  # 30 minutes
INSTANCE_ZERO_NOTIFY_DEBOUNCE_SECS = 60

_last_widget_push = {"mode": None, "active": None}
_last_instance_zero_notify_at = 0.0
_PAVLOK_STIMULUS_TYPES = ("zap", "beep", "vibe")
_pavlok_dispatch_lock = threading.Lock()
_last_pavlok_dispatch_monotonic: float | None = None


def _persist_twitter_zap_cooldown():
    """Write twitter zap wall-clock time to file so it survives restarts."""
    try:
        TWITTER_ZAP_COOLDOWN_FILE.write_text(str(time.time()))
    except Exception as e:
        print(f"WARN: Failed to persist twitter zap cooldown: {e}")


def _restore_twitter_zap_cooldown():
    """On startup, restore twitter zap cooldown from file.
    If a zap happened less than 30 min ago, set twitter_zapped=True to block phantom opens."""
    try:
        if TWITTER_ZAP_COOLDOWN_FILE.exists():
            last_zap_wall = float(TWITTER_ZAP_COOLDOWN_FILE.read_text().strip())
            elapsed = time.time() - last_zap_wall
            if elapsed < TWITTER_ZAP_COOLDOWN_SECS:
                PHONE_STATE["twitter_zapped"] = True
                PHONE_STATE["twitter_last_zap_wall"] = last_zap_wall
                print(
                    f"STARTUP: Twitter zap cooldown restored ({elapsed:.0f}s ago, {TWITTER_ZAP_COOLDOWN_SECS - elapsed:.0f}s remaining). Phantom opens blocked."
                )
            else:
                print(f"STARTUP: Twitter zap cooldown expired ({elapsed:.0f}s ago). Clearing file.")
                TWITTER_ZAP_COOLDOWN_FILE.unlink(missing_ok=True)
    except Exception as e:
        print(f"WARN: Failed to restore twitter zap cooldown: {e}")


def push_phone_widget(mode: str, active_count: int):
    """Push timer mode + active instance count to the phone widget endpoint."""
    if _last_widget_push["mode"] == mode and _last_widget_push["active"] == active_count:
        return

    host = PHONE_CONFIG["host"]
    port = PHONE_CONFIG["port"]
    timeout = PHONE_CONFIG["timeout"]
    url = f"http://{host}:{port}/widget-update?mode={mode}&instances={active_count}"

    try:
        response = requests.get(url, timeout=timeout)
        _last_widget_push["mode"] = mode
        _last_widget_push["active"] = active_count
        print(f"WIDGET: Pushed mode={mode} instances={active_count} -> {response.status_code}")
    except Exception as e:
        print(f"WIDGET: Push failed: {e}")


async def push_phone_widget_async(mode: str, active_count: int):
    """Async wrapper for push_phone_widget."""
    await asyncio.to_thread(push_phone_widget, mode, active_count)


def _send_to_phone_raw(endpoint: str, params: dict | None = None) -> dict:
    """Send params to the phone's MacroDroid HTTP endpoint without Pavlok rewriting."""
    params = params or {}
    host = PHONE_CONFIG["host"]
    port = PHONE_CONFIG["port"]
    timeout = PHONE_CONFIG["timeout"]
    url = f"http://{host}:{port}{endpoint}"

    if endpoint == "/enforce" and PHONE_STATE.get("reachable") is False:
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        print(f"PHONE v3: {endpoint} skipped; phone is known offline")
        return {
            "success": False,
            "skipped_phone": True,
            "reason": "phone_known_offline",
        }

    try:
        response = requests.get(url, params=params, timeout=timeout)
        PHONE_STATE["reachable"] = True
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        print(f"PHONE v3: {endpoint} params={params} -> {response.status_code}")
        return {"success": response.status_code == 200, "status_code": response.status_code}
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        PHONE_STATE["reachable"] = False
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        print(f"PHONE v3: {endpoint} UNREACHABLE: {e}")
        return {"success": False, "error": type(e).__name__}
    except Exception as e:
        PHONE_STATE["reachable"] = False
        print(f"PHONE v3: {endpoint} ERROR: {e}")
        return {"success": False, "error": str(e)}


def _send_to_phone(endpoint: str, params: dict | None) -> dict:
    """Send params to the phone, routing Pavlok keys through the single-lane queue.

    Public callers may still pass legacy v3 `/notify` or `/enforce` payloads that
    contain Pavlok params (`zap`, `beep`, `vibe`). Those params are removed from
    the phone notification request and submitted via `send_pavlok_stimulus()`,
    whose phone-primary transport targets the lightweight `/zap` endpoint.
    """
    params = dict(params or {})
    if endpoint not in ("/notify", "/enforce"):
        return _send_to_phone_raw(endpoint, params)

    notification_params = dict(params)
    pavlok_results: list[dict] = []
    for stimulus_type in _PAVLOK_STIMULUS_TYPES:
        raw_value = notification_params.pop(stimulus_type, None)
        value = _coerce_stimulus_value(raw_value)
        if value is None or value <= 0:
            continue
        pavlok_results.append(
            send_pavlok_stimulus(
                stimulus_type=stimulus_type,
                value=value,
                reason=f"phone_params_{endpoint.strip('/') or 'root'}",
            )
        )

    phone_result: dict | None = None
    if notification_params:
        phone_result = _send_to_phone_raw(endpoint, notification_params)

    if phone_result is None:
        success = all(bool(result.get("success")) for result in pavlok_results) if pavlok_results else True
        return {
            "success": success,
            "status_code": None,
            "phone_skipped": True,
            "reason": "pavlok_only" if pavlok_results else "empty_params",
            "pavlok": pavlok_results,
        }

    if pavlok_results:
        phone_result = {
            **phone_result,
            "pavlok": pavlok_results,
            "success": bool(phone_result.get("success"))
            and all(bool(result.get("success")) for result in pavlok_results),
        }
    return phone_result


def send_pavlok_stimulus(
    stimulus_type: str = "zap",
    value: int | None = None,
    reason: str = "manual",
    respect_cooldown: bool = True,
) -> dict:
    """Queue and send one stimulus (zap/beep/vibe) to the Pavlok watch.

    Phone/MacroDroid `/zap` SendIntent is the primary transport. The Pavlok API
    is fallback-only. A module-level lock serializes all Pavlok actions and a
    minimum inter-stimulus gap prevents concurrent BLE/API actuator races.
    """
    stimulus_type = (stimulus_type or "zap").lower()
    if stimulus_type not in _PAVLOK_STIMULUS_TYPES:
        return {
            "success": False,
            "accepted": False,
            "intent_sent": False,
            "error": f"invalid_stimulus_type:{stimulus_type}",
            "type": stimulus_type,
            "requested_reason": reason,
        }
    if not PAVLOK_CONFIG["enabled"]:
        result = {
            "skipped": True,
            "success": False,
            "accepted": False,
            "intent_sent": False,
            "blocked_by_guardrail": True,
            "reason": "disabled",
            "type": stimulus_type,
            "requested_reason": reason,
        }
        _log_pavlok_guardrail_block(result)
        return result

    now = datetime.now()
    guardrail = _pavlok_guardrail_block(stimulus_type, now, respect_cooldown)
    if guardrail:
        result = {
            "skipped": True,
            "success": False,
            "accepted": False,
            "intent_sent": False,
            "blocked_by_guardrail": True,
            "reason": guardrail["reason"],
            "type": stimulus_type,
            "value": value,
            "requested_reason": reason,
            **{k: v for k, v in guardrail.items() if k != "reason"},
        }
        _log_pavlok_guardrail_block(result)
        return result

    if value is None:
        value = (
            PAVLOK_CONFIG.get("friday_zap_value", 30)
            if stimulus_type == "zap"
            else PAVLOK_CONFIG.get("warning_value", 50)
        )
    value = _normalize_stimulus_value(value)

    with _pavlok_dispatch_lock:
        # Re-check cooldown/cap after waiting for the lane, so concurrent
        # callers cannot all pass the pre-flight guardrail against stale state.
        now = datetime.now()
        guardrail = _pavlok_guardrail_block(stimulus_type, now, respect_cooldown)
        if guardrail:
            result = {
                "skipped": True,
                "success": False,
                "accepted": False,
                "intent_sent": False,
                "blocked_by_guardrail": True,
                "reason": guardrail["reason"],
                "type": stimulus_type,
                "value": value,
                "requested_reason": reason,
                **{k: v for k, v in guardrail.items() if k != "reason"},
            }
            _log_pavlok_guardrail_block(result)
            return result

        _sleep_for_pavlok_min_gap()
        phone_result = _send_pavlok_to_phone(stimulus_type, value)
        if phone_result.get("success"):
            accepted_at = datetime.now()
            _record_pavlok_accepted(stimulus_type, accepted_at)
            _mark_pavlok_dispatched()
            print(f"PAVLOK: phone {stimulus_type} value={value} reason={reason} accepted")
            return {
                "success": True,
                "accepted": True,
                "intent_sent": True,
                "transport": "phone",
                "type": stimulus_type,
                "value": value,
                "reason": reason,
                "phone": phone_result,
            }

        api_result = _send_pavlok_api(stimulus_type, value, reason)
        if api_result.get("success"):
            accepted_at = datetime.now()
            _record_pavlok_accepted(stimulus_type, accepted_at)
        _mark_pavlok_dispatched()
        return {
            **api_result,
            "accepted": bool(api_result.get("success")),
            "intent_sent": False,
            "transport": "api" if api_result.get("success") else "none",
            "type": stimulus_type,
            "value": value,
            "reason": reason,
            "phone": phone_result,
        }


def _send_pavlok_to_phone(stimulus_type: str, value: int) -> dict:
    """Primary Pavlok transport: phone MacroDroid `/zap` → Pavlok SendIntent."""
    if PHONE_STATE.get("reachable") is False:
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        print("PAVLOK: phone dispatch skipped; phone is known offline")
        return {
            "success": False,
            "skipped_phone": True,
            "reason": "phone_known_offline",
        }
    return _send_to_phone_raw("/zap", {"action": stimulus_type, "intensity": value})


def _send_pavlok_api(stimulus_type: str, value: int, reason: str) -> dict:
    """Fallback Pavlok API transport."""
    if not PAVLOK_CONFIG["token"]:
        print(f"PAVLOK: API fallback unavailable for {stimulus_type}: no token")
        return {
            "success": False,
            "accepted": False,
            "intent_sent": False,
            "error": "no_token",
            "reason": reason,
            "hint": "Set PAVLOK_API_TOKEN in .env",
        }
    try:
        response = requests.post(
            PAVLOK_CONFIG["api_url"],
            headers={"Authorization": PAVLOK_CONFIG["token"]},
            json={"stimulus": {"stimulusType": stimulus_type, "stimulusValue": value}},
            timeout=10,
        )
        print(f"PAVLOK: {stimulus_type} value={value} reason={reason} -> {response.status_code}")
        return {
            "success": response.status_code == 200,
            "api_accepted": response.status_code == 200,
            "type": stimulus_type,
            "value": value,
            "reason": reason,
            "status_code": response.status_code,
        }
    except requests.exceptions.Timeout:
        print(f"PAVLOK: Timeout sending {stimulus_type}")
        return {"success": False, "error": "timeout", "reason": reason}
    except requests.exceptions.ConnectionError:
        print(f"PAVLOK: Connection error sending {stimulus_type}")
        return {"success": False, "error": "connection_error", "reason": reason}
    except Exception as e:
        print(f"PAVLOK: Error sending {stimulus_type}: {e}")
        return {"success": False, "error": str(e), "reason": reason}


def _sleep_for_pavlok_min_gap() -> None:
    min_gap = float(PAVLOK_CONFIG.get("min_gap_seconds", 2.0) or 0.0)
    if min_gap <= 0:
        return
    if _last_pavlok_dispatch_monotonic is None:
        return
    elapsed = time.monotonic() - _last_pavlok_dispatch_monotonic
    if elapsed < min_gap:
        delay = min_gap - elapsed
        print(f"PAVLOK: waiting {delay:.2f}s for single-lane min gap")
        time.sleep(delay)


def _mark_pavlok_dispatched() -> None:
    global _last_pavlok_dispatch_monotonic
    _last_pavlok_dispatch_monotonic = time.monotonic()


def _record_pavlok_accepted(stimulus_type: str, accepted_at: datetime) -> None:
    PAVLOK_STATE["last_stimulus_at"] = accepted_at.isoformat()
    if stimulus_type == "zap":
        PAVLOK_STATE["last_zap_at"] = accepted_at.isoformat()
        _increment_daily_zap_count(accepted_at)
    else:
        PAVLOK_STATE["last_soft_at"] = accepted_at.isoformat()


def _coerce_stimulus_value(raw_value) -> int | None:
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _normalize_stimulus_value(raw_value) -> int:
    value = _coerce_stimulus_value(raw_value)
    if value is None:
        return 1
    return max(1, min(100, value))


def _pavlok_guardrail_block(
    stimulus_type: str,
    now: datetime,
    respect_cooldown: bool,
) -> dict | None:
    """Return a guardrail block reason, or None when stimulus is allowed."""
    global_mode = (TTS_GLOBAL_MODE.get("mode") or "").lower()
    if global_mode in ("muted", "silent", "quiet"):
        return {"reason": "quiet_mode", "global_mode": global_mode}
    if DESKTOP_STATE.get("in_meeting"):
        return {"reason": "meeting"}
    if DESKTOP_STATE.get("work_mode") == "sleeping":
        return {"reason": "sleep_window"}

    blocked_contexts = {
        "club": DESKTOP_STATE.get("club_context") or PHONE_STATE.get("club_context"),
        "driving": DESKTOP_STATE.get("driving_context") or PHONE_STATE.get("driving_context"),
        "medical": DESKTOP_STATE.get("medical_context") or PHONE_STATE.get("medical_context"),
    }
    location_zone = (DESKTOP_STATE.get("location_zone") or "").lower()
    for context, active in blocked_contexts.items():
        if active or location_zone == context:
            return {"reason": f"{context}_context"}

    if stimulus_type == "zap":
        _roll_daily_zap_count(now)
        cap = int(PAVLOK_CONFIG.get("daily_zap_cap", 6))
        if int(PAVLOK_STATE.get("zap_count") or 0) >= cap:
            return {"reason": "daily_zap_cap", "cap": cap}

    if not respect_cooldown:
        return None

    if stimulus_type == "zap":
        last_key = "last_zap_at"
        cooldown = int(PAVLOK_CONFIG.get("zap_cooldown_seconds", 20 * 60))
    else:
        last_key = "last_soft_at"
        cooldown = int(PAVLOK_CONFIG.get("soft_cooldown_seconds", 3 * 60))

    last_at = PAVLOK_STATE.get(last_key) or PAVLOK_STATE.get("last_stimulus_at")
    if last_at:
        elapsed = (now - datetime.fromisoformat(last_at)).total_seconds()
        if elapsed < cooldown:
            return {"reason": "cooldown", "remaining": round(cooldown - elapsed)}

    return None


def _in_sleep_window(now: datetime) -> bool:
    return now.hour >= 23 or now.hour < 7


def _roll_daily_zap_count(now: datetime) -> None:
    today = now.date().isoformat()
    if PAVLOK_STATE.get("zap_count_date") != today:
        PAVLOK_STATE["zap_count_date"] = today
        PAVLOK_STATE["zap_count"] = 0


def _increment_daily_zap_count(now: datetime) -> None:
    _roll_daily_zap_count(now)
    PAVLOK_STATE["zap_count"] = int(PAVLOK_STATE.get("zap_count") or 0) + 1


def _log_pavlok_guardrail_block(result: dict) -> None:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(log_event("pavlok_blocked_by_guardrail", details=result))
        else:
            asyncio.run(log_event("pavlok_blocked_by_guardrail", details=result))
    except Exception as exc:
        logger.warning(f"PAVLOK: guardrail block logging failed: {exc}")


async def check_instance_count_pavlok(remaining_active: int, was_active: int):
    """Send Pavlok signals when Claude instance count drops critically."""
    global _last_instance_zero_notify_at

    if remaining_active == 1 and was_active >= 2:
        print(f"INSTANCE COUNT: Dropped to 1 (from {was_active}), double vibe")
        result = await asyncio.to_thread(
            _send_to_phone,
            "/notify",
            {
                "vibe": 50,
                "banner_text": f"1 Claude remaining (was {was_active})",
            },
        )
        if not result["success"]:
            send_pavlok_stimulus(
                stimulus_type="vibe",
                value=50,
                reason="one_claude_remaining",
                respect_cooldown=False,
            )
        await asyncio.sleep(3)
        result = await asyncio.to_thread(_send_to_phone, "/notify", {"vibe": 50})
        if not result["success"]:
            send_pavlok_stimulus(
                stimulus_type="vibe",
                value=50,
                reason="one_claude_remaining",
                respect_cooldown=False,
            )
        await log_event("instance_count_warning", details={"remaining": 1, "was": was_active})
    elif remaining_active == 0 and was_active >= 1:
        now = time.monotonic()
        elapsed = now - _last_instance_zero_notify_at
        if elapsed < INSTANCE_ZERO_NOTIFY_DEBOUNCE_SECS:
            print(
                "INSTANCE COUNT: All Claude instances stopped, "
                f"suppressed duplicate ({elapsed:.1f}s since last)"
            )
            await log_event(
                "instance_count_zero_suppressed",
                details={"was": was_active, "elapsed_seconds": round(elapsed, 1)},
            )
            return

        _last_instance_zero_notify_at = now
        print("INSTANCE COUNT: All Claude instances stopped, zap")
        result = await asyncio.to_thread(
            _send_to_phone,
            "/notify",
            {
                "vibe": 80,
                "beep": 50,
                "tts_text": "All Claude instances stopped",
                "banner_text": "All Claudes stopped",
            },
        )
        if not result["success"]:
            send_pavlok_stimulus(
                stimulus_type="zap",
                value=50,
                reason="all_claudes_stopped",
                respect_cooldown=False,
            )
        await log_event("instance_count_zero", details={"was": was_active})
