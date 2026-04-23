"""Phone and Pavlok transport helpers.

Extracted from main.py so route modules can import notification primitives
without reaching back through the _main() lazy import.
"""

import asyncio
import logging
from datetime import datetime

import requests

from shared import (
    PHONE_CONFIG,
    PHONE_STATE,
    PAVLOK_CONFIG,
    PAVLOK_STATE,
    log_event,
)

logger = logging.getLogger("token_api")

_last_widget_push = {"mode": None, "active": None}


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


def _send_to_phone(endpoint: str, params: dict) -> dict:
    """Send v3 params to the phone's MacroDroid HTTP endpoint."""
    host = PHONE_CONFIG["host"]
    port = PHONE_CONFIG["port"]
    timeout = PHONE_CONFIG["timeout"]
    url = f"http://{host}:{port}{endpoint}"

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


def send_pavlok_stimulus(
    stimulus_type: str = "zap",
    value: int | None = None,
    reason: str = "manual",
    respect_cooldown: bool = True,
) -> dict:
    """Send a stimulus (zap/beep/vibe) to the Pavlok watch."""
    if not PAVLOK_CONFIG["token"]:
        return {"skipped": True, "reason": "no_token", "hint": "Set PAVLOK_API_TOKEN in .env"}
    if not PAVLOK_CONFIG["enabled"]:
        return {"skipped": True, "reason": "disabled"}

    now = datetime.now()
    if respect_cooldown and PAVLOK_STATE["last_stimulus_at"]:
        last = datetime.fromisoformat(PAVLOK_STATE["last_stimulus_at"])
        elapsed = (now - last).total_seconds()
        if elapsed < PAVLOK_CONFIG["cooldown_seconds"]:
            return {
                "skipped": True,
                "reason": "cooldown",
                "remaining": round(PAVLOK_CONFIG["cooldown_seconds"] - elapsed),
            }

    if value is None:
        value = PAVLOK_CONFIG["default_zap_value"]

    try:
        response = requests.post(
            PAVLOK_CONFIG["api_url"],
            headers={"Authorization": PAVLOK_CONFIG["token"]},
            json={"stimulus": {"stimulusType": stimulus_type, "stimulusValue": value}},
            timeout=10,
        )
        PAVLOK_STATE["last_stimulus_at"] = now.isoformat()
        print(f"PAVLOK: {stimulus_type} value={value} reason={reason} -> {response.status_code}")
        return {
            "success": response.status_code == 200,
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


async def check_instance_count_pavlok(remaining_active: int, was_active: int):
    """Send Pavlok signals when Claude instance count drops critically."""
    if remaining_active == 1 and was_active >= 2:
        print(f"INSTANCE COUNT: Dropped to 1 (from {was_active}), double vibe")
        result = await asyncio.to_thread(_send_to_phone, "/notify", {
            "vibe": 50,
            "banner_text": f"1 Claude remaining (was {was_active})",
        })
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
        print("INSTANCE COUNT: All Claude instances stopped, zap")
        result = await asyncio.to_thread(_send_to_phone, "/notify", {
            "vibe": 80,
            "beep": 50,
            "tts_text": "All Claude instances stopped",
            "banner_text": "All Claudes stopped",
        })
        if not result["success"]:
            send_pavlok_stimulus(
                stimulus_type="zap",
                value=50,
                reason="all_claudes_stopped",
                respect_cooldown=False,
            )
        await log_event("instance_count_zero", details={"was": was_active})
