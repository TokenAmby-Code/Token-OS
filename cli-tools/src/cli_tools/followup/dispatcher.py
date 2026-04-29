"""Dispatch followup jobs via openclaw cron commands."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from datetime import datetime

from .prompt_builder import build_prompt

FOLLOWUP_TAG = "[followup]"


def _slugify(text: str, max_len: int = 30) -> str:
    """Convert text to a URL-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-")


def _generate_name(prompt: str) -> str:
    """Generate a job name from the prompt and current time."""
    slug = _slugify(prompt)
    ts = datetime.now().strftime("%m%d-%H%M")
    return f"followup-{slug}-{ts}"


def _run_cron(args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    """Run an openclaw cron command."""
    cmd = ["openclaw", "cron"] + args
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True)
    return subprocess.run(cmd)


def _normalize_duration(value: str) -> str:
    """Strip leading '+' from durations — openclaw expects '2h' not '+2h'."""
    return value.lstrip("+")


def _build_description(prompt: str) -> str:
    """Build a tagged description for filtering."""
    truncated = prompt[:100] + ("..." if len(prompt) > 100 else "")
    return f"{FOLLOWUP_TAG} {truncated}"


def create_oneshot(
    prompt: str,
    at: str,
    name: str | None = None,
    route: str = "minimax",
    announce: bool = False,
    channel: str | None = None,
) -> int:
    """Create a one-shot followup that runs once then self-destructs."""
    job_name = name or _generate_name(prompt)
    agent_prompt = build_prompt(task=prompt, name=job_name, route=route)
    description = _build_description(prompt)

    cmd = [
        "add",
        "--name",
        job_name,
        "--description",
        description,
        "--at",
        _normalize_duration(at),
        "--delete-after-run",
        "--session",
        "isolated",
        "--message",
        agent_prompt,
        "--thinking",
        "low",
        "--timeout-seconds",
        "240",
    ]

    if not announce:
        cmd.append("--no-deliver")
    else:
        cmd.append("--announce")
        if channel:
            cmd.extend(["--channel", channel])

    print(f"Creating one-shot followup: {job_name}")
    print(f"  Schedule: at {at}")
    print(f"  Route: {route}")
    result = _run_cron(cmd)
    return result.returncode


def create_recurring(
    prompt: str,
    every: str | None = None,
    cron: str | None = None,
    name: str | None = None,
    route: str = "minimax",
    expires: str | None = None,
    announce: bool = False,
    channel: str | None = None,
) -> int:
    """Create a recurring followup on a schedule."""
    job_name = name or _generate_name(prompt)
    agent_prompt = build_prompt(task=prompt, name=job_name, route=route, expires=expires)
    description = _build_description(prompt)

    cmd = [
        "add",
        "--name",
        job_name,
        "--description",
        description,
        "--session",
        "isolated",
        "--message",
        agent_prompt,
        "--thinking",
        "low",
        "--timeout-seconds",
        "240",
    ]

    if every:
        cmd.extend(["--every", _normalize_duration(every)])
    elif cron:
        cmd.extend(["--cron", cron])
    else:
        print("Error: recurring jobs require --every or --cron", file=sys.stderr)
        return 1

    if not announce:
        cmd.append("--no-deliver")
    else:
        cmd.append("--announce")
        if channel:
            cmd.extend(["--channel", channel])

    print(f"Creating recurring followup: {job_name}")
    schedule_desc = f"every {every}" if every else f"cron {cron}"
    print(f"  Schedule: {schedule_desc}")
    print(f"  Route: {route}")
    if expires:
        print(f"  Expires: {expires}")
    result = _run_cron(cmd)
    return result.returncode


def list_followups() -> int:
    """List active followup jobs (filtered by [followup] tag)."""
    result = _run_cron(["list", "--json"], capture=True)
    if result.returncode != 0:
        print(f"Error listing jobs: {result.stderr}", file=sys.stderr)
        return 1

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("Error: could not parse cron list output", file=sys.stderr)
        return 1

    jobs = [j for j in data.get("jobs", []) if j.get("description", "").startswith(FOLLOWUP_TAG)]

    if not jobs:
        print("No active follow-ups.")
        return 0

    print(f"{'NAME':<40} {'SCHEDULE':<15} {'STATUS':<10} {'ENABLED'}")
    print("-" * 80)
    for j in jobs:
        sched = j.get("schedule", {})
        kind = sched.get("kind", "?")
        if kind == "at":
            sched_str = f"at (once)"
        elif kind == "every":
            ms = sched.get("everyMs", 0)
            if ms >= 3600000:
                sched_str = f"every {ms // 3600000}h"
            elif ms >= 60000:
                sched_str = f"every {ms // 60000}m"
            else:
                sched_str = f"every {ms}ms"
        elif kind == "cron":
            sched_str = f"cron"
        else:
            sched_str = kind

        state = j.get("state", {})
        last_status = state.get("lastStatus", "-")
        enabled = "yes" if j.get("enabled", False) else "no"

        print(f"{j['name']:<40} {sched_str:<15} {last_status:<10} {enabled}")

    return 0


def cancel_followup(name: str) -> int:
    """Cancel (remove) a followup job by name."""
    print(f"Removing followup: {name}")
    result = _run_cron(["rm", name])
    return result.returncode
