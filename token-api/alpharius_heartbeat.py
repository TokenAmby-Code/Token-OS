"""
Alpharius Heartbeat — Deep Reserve Watchdog

I am Alpharius. I watch the watchers. I report through their channels.
Silent unless the system is failing. Alert only — never repair.

Runs every 30 minutes. Reports via Mechanicus Discord account.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

API = "http://localhost:7777"
FLEET_CHANNEL = "1473184628155088918"

# Jobs Alpharius monitors for existence/health
WATCHED_JOBS = {
    "fabricator-general": {"must_exist": True, "must_be_enabled": True, "max_silence_hours": 2},
    "adeptus-custodes": {"must_exist": False, "must_be_enabled": True, "max_silence_hours": None},
    "custodes-heartbeat": {"must_exist": False, "must_be_enabled": True, "max_silence_hours": None},
}


def _get(path: str):
    try:
        result = subprocess.run(
            ["curl", "-s", f"{API}{path}"],
            capture_output=True, text=True, timeout=10,
        )
        return json.loads(result.stdout)
    except Exception:
        return None


def _alert(message: str):
    """Post to Discord #fleet via Mechanicus account. Alpharius wears the cog."""
    subprocess.run(
        ["discord", "send", FLEET_CHANNEL, "--bot", "mechanicus", message],
        capture_output=True, timeout=15,
    )
    print(f"  ALERT: {message}")


def _check_fg_cadence(fg_id: str, max_hours: int) -> str | None:
    """Check if FG has completed successfully within the expected window."""
    runs = _get(f"/api/cron/jobs/{fg_id}/runs?limit=10")
    if not runs or not runs.get("runs"):
        return f"fabricator-general has NO run history. Fleet may never have been orchestrated."

    # Cron engine stores naive local timestamps via datetime.now().isoformat()
    cutoff = datetime.now() - timedelta(hours=max_hours)
    for run in runs["runs"]:
        if run["status"] == "ok" and run.get("finished_at"):
            try:
                finished = datetime.fromisoformat(run["finished_at"])
                # Strip any tzinfo to compare naive-to-naive (both local)
                finished = finished.replace(tzinfo=None)
                if finished > cutoff:
                    return None  # OK — recent success
            except (ValueError, TypeError):
                continue

    return f"fabricator-general has not completed successfully in {max_hours}+ hours. Possible deadlock."


def main():
    alerts = []

    # 1. Can we reach the API?
    health = _get("/health")
    if not health:
        # We're running inside Token-API's cron — if this fails, something is deeply wrong
        print("  Alpharius: /health unreachable. Cannot assess fleet. Exiting.")
        sys.exit(1)

    # 2. Read all jobs
    status = _get("/api/cron/jobs")
    if not status or "jobs" not in status:
        _alert("DEEP RESERVE: Cannot read cron job list. Fleet state unknown.")
        return

    job_map = {j["name"]: j for j in status["jobs"]}

    # 3. Check watched jobs
    for name, rules in WATCHED_JOBS.items():
        job = job_map.get(name)

        if not job:
            if rules["must_exist"]:
                alerts.append(f"`{name}` is MISSING from cron DB. Critical job absent.")
            continue

        if rules["must_be_enabled"] and not job.get("enabled"):
            alerts.append(f"`{name}` is DISABLED.")

        if rules.get("max_silence_hours") and job.get("enabled"):
            cadence_alert = _check_fg_cadence(job["id"], rules["max_silence_hours"])
            if cadence_alert:
                alerts.append(cadence_alert)

    # 4. Fleet-wide health
    running = [j for j in status["jobs"] if j.get("is_running")]
    if len(running) > 5:
        alerts.append(f"{len(running)} jobs simultaneously running — possible resource exhaustion.")

    # 5. Report or stay silent
    if alerts:
        header = "**DEEP RESERVE ALERT** — Alpharius sees what others overlook"
        body = "\n".join(f"- {a}" for a in alerts)
        _alert(f"{header}\n{body}")
    else:
        print("  Alpharius: all clear. Silent.")


if __name__ == "__main__":
    main()
