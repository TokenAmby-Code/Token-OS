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
import re
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
        "token_os_runtime": "~/runtimes/Token-OS/live",
    },
    "wsl": {
        "nas_imperium": "/mnt/imperium",
        "nas_civic": "/mnt/civic",
        "tailscale_ip": "100.66.10.74",
        "token_api_url": "http://100.95.109.23:7777",
        "ssh_alias": "wsl",
        "device_name": "TokenPC",
        "token_os_runtime": "/home/token/runtimes/token-os/live",
    },
    "phone": {
        "nas_imperium": "",
        "nas_civic": "",
        "tailscale_ip": "100.102.92.24",
        "token_api_url": "http://100.95.109.23:7777",
        "ssh_alias": "phone",
        "device_name": "Token-S24",
        "token_os_runtime": "",
    },
    "linux": {
        "nas_imperium": "/mnt/imperium",
        "nas_civic": "/mnt/civic",
        "tailscale_ip": "",
        "token_api_url": "http://100.95.109.23:7777",
        "ssh_alias": "",
        "device_name": "",
        "token_os_runtime": "/home/token/runtimes/token-os/live",
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


_QUARANTINE_RE = re.compile(r"\.legacy-\d")


def _is_quarantined(path: str) -> bool:
    """True for paths that must NEVER win runtime/bare resolution.

    A Synology recycle bin (``#recycle``), a macOS Trash, or a dated legacy
    archive (``…legacy-YYYYMMDD``) is a purge target. Binding the runtime — or a
    worktree's bare — there silently destroys work when the bin is emptied
    (incident 2026-06-22). Mirrors imperium_path_is_quarantined in nas-path.sh.
    """
    if not path:
        return False
    norm = "/" + path.strip("/") + "/"
    if "/#recycle/" in norm or "/.Trash/" in norm or "/.Trashes/" in norm:
        return True
    return bool(_QUARANTINE_RE.search(norm))


def _runtime_checkout() -> str:
    local = os.path.expanduser(cfg("token_os_runtime").strip())
    env_value = (os.environ.get("TOKEN_OS") or "").strip()
    env_expanded = os.path.expanduser(env_value) if env_value else ""
    known_nas_runtime = f"{IMPERIUM}/runtimes/token-os/live"

    # A quarantined override (recycle bin / dated legacy archive) is never honored,
    # even when the dir still exists: a stale exported TOKEN_OS pointing into
    # #recycle previously won here, binding tooling + worktrees to a purge target.
    if env_value and _is_quarantined(env_expanded):
        env_value = env_expanded = ""

    # Explicit non-NAS overrides still work for tests/dev, but a stale exported
    # NAS runtime must not beat the machine-local hot runtime during cutover.
    if env_value and os.path.isdir(env_expanded) and env_value != known_nas_runtime:
        return env_expanded
    if local and os.path.isdir(local) and not _is_quarantined(local):
        return local
    if env_value and os.path.isdir(env_expanded):
        return env_expanded
    return known_nas_runtime


TOKEN_OS = _runtime_checkout()
CLI_TOOLS = f"{TOKEN_OS}/cli-tools"
TOKEN_API_URL = os.environ.get("TOKEN_API_URL") or cfg("token_api_url")

# All Tailscale IPs for device resolution (replaces DEVICE_IPS in main.py)
DEVICE_IPS: dict[str, str] = {}
for _m, _c in _REGISTRY.items():
    if _c["tailscale_ip"]:
        DEVICE_IPS[_c["tailscale_ip"]] = _c["device_name"]
DEVICE_IPS["127.0.0.1"] = "Mac-Mini"  # localhost = mac
