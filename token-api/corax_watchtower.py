"""
Corax Watchtower — Terra Deep Watch

I see what others overlook. The anomaly in the pattern.
The silence where there should be noise.

Deterministic Python watcher for Terra domain.
Watches deep logs, file integrity, anomaly detection.
Reports problems. Silent when clean.

Runs every 30 minutes. commander=dorn.
Modeled after alpharius_heartbeat.py.
"""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

API = "http://localhost:7777"
FLEET_CHANNEL = "1473184628155088918"
VAULT_ROOT = Path.home() / "Imperium-ENV"
STATE_FILE = Path.home() / ".claude" / "corax-state.json"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent

# Critical paths to monitor for unexpected changes
WATCHED_PATHS = {
    "cron_engine": SCRIPTS_DIR / "token-api" / "cron_engine.py",
    "main_py": SCRIPTS_DIR / "token-api" / "main.py",
    "stop_hook": SCRIPTS_DIR / "token-api" / "stop_hook.py",
    "alpharius": SCRIPTS_DIR / "token-api" / "alpharius_heartbeat.py",
    "discord_daemon": Path.home() / ".discord-cli" / "node" / "daemon.js",
}

# Vault directories to check for anomalies
VAULT_DIRS = {
    "inbox": VAULT_ROOT / "Terra" / "Inbox",
    "sessions_terra": VAULT_ROOT / "Terra" / "Sessions",
    "sessions_mars": VAULT_ROOT / "Mars" / "Sessions",
    "tasks": VAULT_ROOT / "Mars" / "Tasks",
}


def _load_state() -> dict:
    """Load previous state for diffing."""
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"timestamp": "never", "file_hashes": {}, "vault_counts": {}}


def _save_state(state: dict):
    """Persist state for next cycle."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _get(path: str):
    """Query Token-API."""
    try:
        result = subprocess.run(
            ["curl", "-s", f"{API}{path}"],
            capture_output=True, text=True, timeout=10,
        )
        return json.loads(result.stdout)
    except Exception:
        return None


def _alert(message: str):
    """Post to Discord #fleet. Corax speaks through Mechanicus channels (for now)."""
    subprocess.run(
        ["discord", "send", FLEET_CHANNEL, "--bot", "mechanicus", message],
        capture_output=True, timeout=15,
    )
    print(f"  ALERT: {message}")


def _hash_file(path: Path) -> str | None:
    """SHA-256 hash of a file, or None if missing."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except (FileNotFoundError, PermissionError):
        return None


def _count_files(directory: Path) -> int:
    """Count .md files in a directory."""
    try:
        return len(list(directory.glob("*.md")))
    except (FileNotFoundError, PermissionError):
        return -1


def check_infrastructure_integrity(prev_state: dict) -> list[str]:
    """Check critical infrastructure files for unexpected modifications."""
    alerts = []
    current_hashes = {}
    prev_hashes = prev_state.get("file_hashes", {})

    for name, path in WATCHED_PATHS.items():
        h = _hash_file(path)
        current_hashes[name] = h

        if h is None:
            alerts.append(f"MISSING: `{name}` ({path}) — critical infrastructure file absent")
        elif name in prev_hashes and prev_hashes[name] != h and prev_hashes[name] is not None:
            alerts.append(f"MODIFIED: `{name}` changed since last scan (was {prev_hashes[name]}, now {h})")

    return alerts, current_hashes


def check_vault_anomalies(prev_state: dict) -> list[str]:
    """Check vault directories for count anomalies."""
    alerts = []
    current_counts = {}
    prev_counts = prev_state.get("vault_counts", {})

    for name, directory in VAULT_DIRS.items():
        count = _count_files(directory)
        current_counts[name] = count

        if count == -1:
            alerts.append(f"INACCESSIBLE: vault directory `{name}` ({directory})")
        elif name in prev_counts and prev_counts[name] >= 0:
            delta = count - prev_counts[name]
            # Large swings are suspicious
            if abs(delta) > 20:
                alerts.append(f"VAULT ANOMALY: `{name}` changed by {delta:+d} files ({prev_counts[name]} → {count})")

    return alerts, current_counts


def check_token_api_health() -> list[str]:
    """Verify Token-API is responsive and healthy."""
    alerts = []

    health = _get("/health")
    if not health:
        alerts.append("Token-API /health unreachable — all fleet operations compromised")
        return alerts

    # Check DB connectivity via a lightweight query
    jobs = _get("/api/cron/jobs")
    if not jobs or "jobs" not in jobs:
        alerts.append("Token-API cron jobs endpoint failing — fleet state unknown")

    return alerts


def check_stale_sessions() -> list[str]:
    """Flag session docs that have been 'active' for too long (potential orphans)."""
    alerts = []

    for sessions_dir in [VAULT_DIRS["sessions_terra"], VAULT_DIRS["sessions_mars"]]:
        try:
            for f in sessions_dir.glob("*.md"):
                try:
                    content = f.read_text(errors="replace")
                    if "status: active" in content:
                        # Check file modification time
                        mtime = datetime.fromtimestamp(f.stat().st_mtime)
                        age = datetime.now() - mtime
                        if age > timedelta(days=3):
                            alerts.append(
                                f"STALE SESSION: `{f.name}` active but untouched for {age.days}d"
                            )
                except (PermissionError, OSError):
                    continue
        except (FileNotFoundError, PermissionError):
            continue

    return alerts


def check_env_files() -> list[str]:
    """Scan for exposed secrets in common locations."""
    alerts = []

    secret_patterns = [
        VAULT_ROOT / ".env",
        VAULT_ROOT / "credentials.json",
        SCRIPTS_DIR / "token-api" / ".env",
    ]

    for p in secret_patterns:
        if p.exists():
            # Check if it's gitignored (in a git repo)
            try:
                result = subprocess.run(
                    ["git", "check-ignore", str(p)],
                    capture_output=True, text=True, timeout=5,
                    cwd=p.parent,
                )
                if result.returncode != 0:
                    # Not gitignored
                    alerts.append(f"EXPOSED SECRET: `{p}` exists and is NOT gitignored")
            except Exception:
                # Not in a git repo — just note the file exists
                alerts.append(f"SECRET FILE: `{p}` exists — verify it is not exposed")

    return alerts


def check_prompt_directory() -> list[str]:
    """Check prompt directory for suspicious patterns."""
    alerts = []
    prompts_dir = Path.home() / ".claude" / "prompts"

    # Patterns that indicate exfiltration or destruction — NOT internal API calls
    DANGEROUS_PATTERNS = [
        "/etc/passwd", "/etc/shadow",
        "rm -rf /", "mkfifo", "reverse shell",
        "wget ", "\nnc ", "netcat ",
        "base64 -d", "eval(", "exec(",
    ]

    try:
        for f in prompts_dir.glob("*.md"):
            content = f.read_text(errors="replace")
            lower = content.lower()
            found = [p for p in DANGEROUS_PATTERNS if p in lower]
            if found:
                alerts.append(
                    f"SUSPICIOUS PROMPT: `{f.name}` contains: {', '.join(found)}"
                )
    except (FileNotFoundError, PermissionError):
        pass

    return alerts


def main():
    prev_state = _load_state()
    all_alerts = []

    # 1. Token-API health (gate check)
    api_alerts = check_token_api_health()
    all_alerts.extend(api_alerts)
    if any("unreachable" in a for a in api_alerts):
        _alert("**CORAX WATCHTOWER** — Token-API unreachable. Cannot complete scan.")
        return

    # 2. Infrastructure file integrity
    infra_alerts, current_hashes = check_infrastructure_integrity(prev_state)
    all_alerts.extend(infra_alerts)

    # 3. Vault directory anomalies
    vault_alerts, current_counts = check_vault_anomalies(prev_state)
    all_alerts.extend(vault_alerts)

    # 4. Stale sessions
    all_alerts.extend(check_stale_sessions())

    # 5. Exposed secrets
    all_alerts.extend(check_env_files())

    # 6. Prompt directory scan
    all_alerts.extend(check_prompt_directory())

    # Save state for next cycle
    _save_state({
        "timestamp": datetime.now().isoformat(),
        "file_hashes": current_hashes,
        "vault_counts": current_counts,
    })

    # Report or stay silent
    if all_alerts:
        header = "**CORAX WATCHTOWER** — The Raven sees what others overlook"
        body = "\n".join(f"- {a}" for a in all_alerts)
        _alert(f"{header}\n{body}")
    else:
        print("  Corax: all clear. Silent.")


if __name__ == "__main__":
    main()
