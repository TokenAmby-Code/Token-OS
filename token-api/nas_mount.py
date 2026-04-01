"""NAS mount detection and recovery utilities.

Used by the cron engine and any script that depends on /Volumes/Imperium or
/Volumes/Civic being available. Attempts to remount via AppleScript (uses
macOS keychain credentials) before giving up.
"""
import os
import subprocess
import time

# Share definitions: (mount_point, smb_uri)
NAS_SHARES = {
    "/Volumes/Imperium": "smb://TokenClaw@Token-NAS._smb._tcp.local/Imperium",
    "/Volumes/Civic":    "smb://TokenClaw@Token-NAS._smb._tcp.local/Civic",
}

# Probe file that must exist and be readable to confirm the mount is live
# (not just a stale ghost directory)
_PROBE_FILES = {
    "/Volumes/Imperium": "/Volumes/Imperium/Imperium-ENV",
    "/Volumes/Civic":    "/Volumes/Civic/Pax-ENV",
}

MOUNT_TIMEOUT_SECONDS = 15   # Max wait after triggering a mount attempt
MOUNT_POLL_INTERVAL  = 1.0   # How often to re-check during wait


def is_mounted(mount_point: str) -> bool:
    """Return True if the share is mounted AND the probe path is accessible."""
    probe = _PROBE_FILES.get(mount_point, mount_point)
    try:
        return os.path.exists(probe)
    except OSError:
        return False


def _trigger_mount(smb_uri: str) -> bool:
    """Trigger a mount via AppleScript (uses macOS keychain, no password prompt).
    Returns True if the osascript call succeeded (not whether the mount is live yet).
    """
    script = f'mount volume "{smb_uri}"'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=20,
        )
        return result.returncode == 0
    except Exception:
        return False


def ensure_mounted(mount_point: str, *, retry: bool = True) -> tuple[bool, str]:
    """Ensure a NAS share is mounted. Returns (success, message).

    If not mounted, attempts one AppleScript remount and waits up to
    MOUNT_TIMEOUT_SECONDS for it to appear. On success returns (True, "").
    On failure returns (False, human-readable reason).
    """
    if is_mounted(mount_point):
        return True, ""

    if not retry:
        return False, f"NAS share {mount_point} not mounted (retry disabled)"

    smb_uri = NAS_SHARES.get(mount_point)
    if not smb_uri:
        return False, f"Unknown mount point: {mount_point}"

    print(f"NAS: {mount_point} not mounted — attempting remount via AppleScript")
    _trigger_mount(smb_uri)

    deadline = time.monotonic() + MOUNT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(MOUNT_POLL_INTERVAL)
        if is_mounted(mount_point):
            print(f"NAS: {mount_point} remounted successfully")
            return True, ""

    return False, (
        f"NAS share {mount_point} unavailable after {MOUNT_TIMEOUT_SECONDS}s. "
        f"NAS may be offline or unreachable."
    )


def shares_needed_for(command: str) -> list[str]:
    """Return mount points referenced by a command string."""
    needed = []
    for mount_point in NAS_SHARES:
        if mount_point in command:
            needed.append(mount_point)
    return needed


def ensure_command_mounts(command: str) -> tuple[bool, str]:
    """Check/recover all NAS shares needed by a command.

    Returns (all_ok, error_message). error_message is "" on success.
    """
    needed = shares_needed_for(command)
    if not needed:
        return True, ""
    for mount_point in needed:
        ok, msg = ensure_mounted(mount_point)
        if not ok:
            return False, msg
    return True, ""
