"""
imperium_config.py — Python equivalent of nas-path.sh machine config.

Reads IMPERIUM_MACHINE and other env vars set by nas-path.sh.
Falls back to platform detection if env vars aren't set (e.g., direct Python invocation).

Usage:
    from imperium_config import cfg, MACHINE, IMPERIUM, TOKEN_API_URL

    phone_ip = cfg("tailscale_ip", "phone")
    nas_path = cfg("nas_imperium")  # current machine
"""

import os
import platform
import sys

# ============================================================
# MACHINE IDENTITY
# ============================================================

def _detect_machine() -> str:
    """Detect machine from env or platform. Matches nas-path.sh logic."""
    env = os.environ.get("IMPERIUM_MACHINE")
    if env:
        return env
    if sys.platform == "darwin":
        return "mac"
    if "microsoft" in platform.uname().release.lower():
        return "wsl"
    if os.path.isdir("/data/data/com.termux"):
        return "phone"
    return "linux"

MACHINE = _detect_machine()

# ============================================================
# CONFIG REGISTRY — mirrors nas-path.sh exactly
# ============================================================

_REGISTRY: dict[str, dict[str, str]] = {
    "mac": {
        "nas_imperium": "/Volumes/Imperium",
        "nas_civic": "/Volumes/Civic",
        "tailscale_ip": "100.95.109.23",
        "token_api_url": "http://localhost:7777",
        "ssh_alias": "mini",
        "device_name": "Mac-Mini",
    },
    "wsl": {
        "nas_imperium": "/mnt/imperium",
        "nas_civic": "/mnt/civic",
        "tailscale_ip": "100.66.10.74",
        "token_api_url": "http://100.95.109.23:7777",
        "ssh_alias": "wsl",
        "device_name": "TokenPC",
    },
    "phone": {
        "nas_imperium": "",
        "nas_civic": "",
        "tailscale_ip": "100.102.92.24",
        "token_api_url": "http://100.95.109.23:7777",
        "ssh_alias": "phone",
        "device_name": "Token-S24",
    },
    "linux": {
        "nas_imperium": "/mnt/imperium",
        "nas_civic": "/mnt/civic",
        "tailscale_ip": "",
        "token_api_url": "http://100.95.109.23:7777",
        "ssh_alias": "",
        "device_name": "",
    },
}

# ============================================================
# LOOKUP
# ============================================================

def cfg(key: str, machine: str | None = None) -> str:
    """Look up a config value. Defaults to current machine."""
    m = machine or MACHINE
    return _REGISTRY.get(m, {}).get(key, "")


# ============================================================
# CONVENIENCE EXPORTS — match shell env vars
# ============================================================

IMPERIUM = os.environ.get("IMPERIUM") or cfg("nas_imperium")
CIVIC = os.environ.get("CIVIC") or cfg("nas_civic")
SCRIPTS = os.environ.get("SCRIPTS") or f"{IMPERIUM}/Scripts"
CLI_TOOLS = os.environ.get("CLI_TOOLS") or f"{SCRIPTS}/cli-tools"
TOKEN_API_URL = os.environ.get("TOKEN_API_URL") or cfg("token_api_url")

# All Tailscale IPs for device resolution (replaces DEVICE_IPS in main.py)
DEVICE_IPS: dict[str, str] = {}
for _m, _c in _REGISTRY.items():
    if _c["tailscale_ip"]:
        DEVICE_IPS[_c["tailscale_ip"]] = _c["device_name"]
DEVICE_IPS["127.0.0.1"] = "Mac-Mini"  # localhost = mac
