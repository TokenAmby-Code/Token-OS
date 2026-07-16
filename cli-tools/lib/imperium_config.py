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
from pathlib import PurePath

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
    # Generic Linux: distinguish the K12 boxes by hostname so the personal/work
    # split stays nameable for routing and enforcement scoping. Any other Linux
    # node stays the generic "linux" fallback.
    host = platform.node().split(".")[0]
    if host in ("k12-personal", "k12-work"):
        return host
    return "linux"


MACHINE = _detect_machine()

# ============================================================
# CONFIG REGISTRY — mirrors nas-path.sh exactly
# ============================================================

# Token-API host — the single tailnet node currently serving Token-API (the mac
# today; migrates to k12-personal at cutover). Hoisted once so satellite rows
# don't each embed the literal IP. Machines that run their OWN local Token-API
# (mac, k12-personal) point at localhost instead of this host.
_IMPERIUM_TOKEN_API_HOST = "100.95.109.23"

_REGISTRY: dict[str, dict[str, str]] = {
    "mac": {
        "nas_imperium": "/Volumes/Imperium",
        "nas_civic": "/Volumes/Civic",
        "tailscale_ip": "100.95.109.23",
        "token_api_url": "http://localhost:7777",
        "tmuxctld_url": "http://127.0.0.1:7778",
        "ssh_alias": "mini",
        "device_name": "Mac-Mini",
        "token_os_runtime": "~/runtimes/Token-OS/live",
        "token_fleet_runtime": "~/runtimes/Token-Fleet/live",
    },
    "wsl": {
        "nas_imperium": "/mnt/imperium",
        "nas_civic": "/mnt/civic",
        "tailscale_ip": "100.66.10.74",
        "token_api_url": f"http://{_IMPERIUM_TOKEN_API_HOST}:7777",
        "tmuxctld_url": "http://127.0.0.1:7778",
        "ssh_alias": "wsl",
        "device_name": "TokenPC",
        "token_os_runtime": "/home/token/runtimes/token-os/live",
        "token_fleet_runtime": "/home/token/runtimes/Token-Fleet/live",
    },
    "phone": {
        "nas_imperium": "",
        "nas_civic": "",
        "tailscale_ip": "100.102.92.24",
        "token_api_url": f"http://{_IMPERIUM_TOKEN_API_HOST}:7777",
        "tmuxctld_url": "http://127.0.0.1:7778",
        "ssh_alias": "phone",
        "device_name": "Token-S24",
        "token_os_runtime": "",
        "token_fleet_runtime": "",
    },
    "linux": {
        "nas_imperium": "/mnt/imperium",
        "nas_civic": "/mnt/civic",
        "tailscale_ip": "",
        "token_api_url": f"http://{_IMPERIUM_TOKEN_API_HOST}:7777",
        "tmuxctld_url": "http://127.0.0.1:7778",
        "ssh_alias": "",
        "device_name": "",
        "token_os_runtime": "/home/token/runtimes/token-os/live",
        "token_fleet_runtime": "/home/token/runtimes/Token-Fleet/live",
    },
    # K12 personal (GMKtec K12; Imperium domain — replaces the Mac Mini). Runs
    # its OWN local Token-API (per-box registry pre-cutover) and is the long-term
    # Token-API home, so token_api_url is localhost. Civic is NOT mounted here:
    # the personal/work boundary is physical — cross-mounting is prohibited.
    "k12-personal": {
        "nas_imperium": "/mnt/imperium",
        "nas_civic": "",
        "tailscale_ip": "100.113.115.32",
        "token_api_url": "http://localhost:7777",
        "tmuxctld_url": "http://127.0.0.1:7778",
        "ssh_alias": "k12-personal",
        "device_name": "K12-Personal",
        "token_os_runtime": "~/runtimes/Token-OS/live",
        "token_fleet_runtime": "~/runtimes/Token-Fleet/live",
    },
    # K12 work (GMKtec K12; Civic/Pax domain — first physical CIVIC_MACHINE).
    # Present in the Imperium registry only to be nameable for routing/enforcement
    # scoping; civic-specific config lives in Pax-ENV. Imperium is NOT mounted on
    # the work box (boundary), and it runs no Token-OS runtime.
    "k12-work": {
        "nas_imperium": "",
        "nas_civic": "/mnt/civic",
        "tailscale_ip": "100.67.168.105",
        "token_api_url": f"http://{_IMPERIUM_TOKEN_API_HOST}:7777",
        "tmuxctld_url": "http://127.0.0.1:7778",
        "ssh_alias": "k12-work",
        "device_name": "K12-Work",
        "token_os_runtime": "",
        "token_fleet_runtime": "",
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
    # Compare normalized expanded paths so a trailing slash or `~` form of the NAS
    # runtime can't slip past this check and wrongly beat the local hot runtime.
    env_norm = os.path.normpath(env_expanded) if env_expanded else ""
    nas_norm = os.path.normpath(known_nas_runtime)
    if env_value and os.path.isdir(env_expanded) and env_norm != nas_norm:
        return env_expanded
    if local and os.path.isdir(local) and not _is_quarantined(local):
        return local
    if env_value and os.path.isdir(env_expanded):
        return env_expanded
    return known_nas_runtime


TOKEN_OS = _runtime_checkout()
CLI_TOOLS = f"{TOKEN_OS}/cli-tools"
TOKEN_FLEET_CHECKOUT = os.path.expanduser(
    os.environ.get("TOKEN_FLEET_CHECKOUT") or cfg("token_fleet_runtime")
)
TOKEN_API_URL = os.environ.get("TOKEN_API_URL") or cfg("token_api_url")
TMUXCTLD_URL = os.environ.get("TMUXCTLD_URL") or cfg("tmuxctld_url")
RUNTIME_DATABASE_DIR = os.path.expanduser(
    os.environ.get("TOKEN_API_DATABASE_DIR") or "~/runtimes/database"
)
_TOKEN_API_DB = os.path.expanduser(os.environ.get("TOKEN_API_DB") or "") or ""

TOKEN_API_AGENTS_DB = os.path.expanduser(
    os.environ.get("TOKEN_API_AGENTS_DB")
    or _TOKEN_API_DB
    or os.path.join(RUNTIME_DATABASE_DIR, "agents.db")
)
TOKEN_API_TIMER_DB = os.path.expanduser(
    os.environ.get("TOKEN_API_TIMER_DB")
    or _TOKEN_API_DB
    or os.path.join(RUNTIME_DATABASE_DIR, "timer.db")
)
TOKEN_API_TELEMETRY_DB = os.path.expanduser(
    os.environ.get("TOKEN_API_TELEMETRY_DB")
    or (str(PurePath(_TOKEN_API_DB).with_name("telemetry.db")) if _TOKEN_API_DB else "")
    or os.path.join(RUNTIME_DATABASE_DIR, "telemetry.db")
)

# All Tailscale IPs for device resolution (replaces DEVICE_IPS in main.py)
DEVICE_IPS: dict[str, str] = {}
for _m, _c in _REGISTRY.items():
    if _c["tailscale_ip"]:
        DEVICE_IPS[_c["tailscale_ip"]] = _c["device_name"]
DEVICE_IPS["127.0.0.1"] = "Mac-Mini"  # localhost = mac
