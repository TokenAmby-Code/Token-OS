"""Desktop enforcement service — closes distraction windows via Windows satellite.

Extracted from main.py so route modules can import these functions directly
instead of reaching back through the _main() lazy import.
"""

import logging
import requests
from datetime import datetime

from shared import DESKTOP_CONFIG, DESKTOP_STATE

logger = logging.getLogger("token_api")


def close_distraction_windows() -> dict:
    """Close distraction windows on Windows via token-satellite.

    Mode-aware enforcement:
    - video mode → close brave (YouTube in browser)
    - gaming mode → close minecraft
    """
    current_mode = DESKTOP_STATE.get("current_mode", "silence")

    mode_targets = {
        "video": ["brave"],
        "gaming": ["minecraft"],
    }

    targets = mode_targets.get(current_mode, [])
    if not targets:
        logger.info(f"ENFORCE: No targets for mode '{current_mode}'")
        return {"success": True, "closed_count": 0, "mode": current_mode}

    results = []
    for app in targets:
        result = enforce_desktop_app(app, "close")
        results.append(result)

    closed = sum(1 for r in results if r.get("success"))
    logger.info(f"ENFORCE: Closed {closed}/{len(targets)} targets for mode '{current_mode}'")
    return {"success": closed > 0 or not targets, "closed_count": closed, "results": results}


def enforce_desktop_app(app_name: str, action: str = "close") -> dict:
    """Send enforcement command to Windows via token-satellite."""
    host = DESKTOP_CONFIG["host"]
    port = DESKTOP_CONFIG["port"]
    timeout = DESKTOP_CONFIG["timeout"]

    url = f"http://{host}:{port}/enforce"

    try:
        response = requests.post(
            url,
            json={"app": app_name, "action": action},
            timeout=timeout,
        )
        logger.info(f"DESKTOP: Enforce {action} {app_name} -> {response.status_code}")
        return {
            "success": response.status_code == 200,
            "app": app_name,
            "status_code": response.status_code,
            "response": response.json() if response.status_code == 200 else response.text,
        }
    except Exception as e:
        logger.error(f"DESKTOP: Error enforcing {action} {app_name}: {e}")
        DESKTOP_STATE["ahk_reachable"] = False
        return {"success": False, "app": app_name, "error": str(e)}


def check_desktop_reachable() -> dict:
    """Check if Windows satellite server is reachable."""
    host = DESKTOP_CONFIG["host"]
    port = DESKTOP_CONFIG["port"]
    timeout = DESKTOP_CONFIG["timeout"]

    url = f"http://{host}:{port}/health"

    try:
        response = requests.get(url, timeout=timeout)
        DESKTOP_STATE["ahk_reachable"] = True
        DESKTOP_STATE["ahk_last_heartbeat"] = datetime.now().isoformat()
        return {"reachable": True, "status_code": response.status_code}
    except Exception:
        DESKTOP_STATE["ahk_reachable"] = False
        return {"reachable": False}
