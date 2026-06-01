"""NOW daily-note widget composer and writer.

Reads current Token-API telemetry directly from SQLite and writes through the
same pure callout writer used by the HTTP endpoint. The scheduler intentionally
skips the HTTP roundtrip: this avoids self-calling the local FastAPI server from
inside its own event loop and keeps the callout primitive as the single write
path.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dailynote_callout import CalloutWriteResult, apply_callout
from pane_surface import human_pane_surface

MST = ZoneInfo("America/Phoenix")


@dataclass(frozen=True)
class NowWidgetTelemetry:
    timer: dict
    active_instances: list[str]
    location_zone: str | None
    desktop_mode: str | None
    recent_cascade: str | None


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def load_telemetry(db_path: str | Path) -> NowWidgetTelemetry:
    db_path = Path(db_path)
    timer: dict = {}
    active_instances: list[str] = []
    location_zone = None
    desktop_mode = None
    recent_cascade = None

    with _connect(db_path) as conn:
        row = conn.execute("SELECT state_json FROM timer_state WHERE id = 1").fetchone()
        if row and row["state_json"]:
            try:
                timer = json.loads(row["state_json"])
            except json.JSONDecodeError:
                timer = {"error": "timer_state JSON decode failed"}

        for row in conn.execute(
            """
            SELECT id, tab_name, working_dir, tmux_pane, pane_label
            FROM claude_instances
            WHERE status IN ('processing', 'idle')
            ORDER BY last_activity DESC
            LIMIT 6
            """
        ):
            surface = human_pane_surface(row["tab_name"], row["tmux_pane"], row["pane_label"])
            if surface == "session":
                surface = row["tmux_pane"] or (
                    Path(row["working_dir"]).name if row["working_dir"] else row["id"]
                )
            active_instances.append(surface or "unknown")

        event_cutoff = (datetime.now(MST) - timedelta(hours=1)).isoformat()
        for row in conn.execute(
            """
            SELECT event_type, details, created_at
            FROM events
            WHERE created_at >= ?
            ORDER BY created_at DESC
            LIMIT 100
            """,
            (event_cutoff,),
        ):
            details = {}
            if row["details"]:
                try:
                    details = json.loads(row["details"])
                except json.JSONDecodeError:
                    details = {}
            etype = row["event_type"] or ""
            if location_zone is None and etype in {"location_event", "geofence"}:
                if details.get("action") == "enter":
                    location_zone = details.get("location")
                elif details.get("current_zone"):
                    location_zone = details.get("current_zone")
            if recent_cascade is None and ("cascade" in etype or etype.startswith("enforcement")):
                recent_cascade = details.get("app") or details.get("reason") or etype
            if location_zone is not None and recent_cascade is not None:
                break

    desktop_mode = timer.get("desktop_mode") or timer.get("activity")
    location_zone = timer.get("location_zone") or location_zone
    return NowWidgetTelemetry(
        timer=timer,
        active_instances=active_instances,
        location_zone=location_zone,
        desktop_mode=desktop_mode,
        recent_cascade=recent_cascade,
    )


def _format_minutes(ms: int | float | None) -> str:
    if ms is None:
        return "unknown"
    minutes = round(float(ms) / 60000)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes)}min"


def compose_now_markdown(
    telemetry: NowWidgetTelemetry,
    *,
    now: datetime | None = None,
) -> str:
    now = now.astimezone(MST) if now else datetime.now(MST)
    timer = telemetry.timer
    mode = (
        timer.get("effective_mode")
        or timer.get("current_mode")
        or timer.get("mode")
        or _derive_timer_mode(timer)
    )
    balance = _format_minutes(timer.get("break_balance_ms", timer.get("accumulated_break_ms", 0)))
    active = ", ".join(telemetry.active_instances) if telemetry.active_instances else "none"
    location = telemetry.location_zone or "unknown"
    desktop = telemetry.desktop_mode or "unknown"
    cascade = telemetry.recent_cascade or "none in last hour"

    return "\n".join(
        [
            f"**Block:** {now.strftime('%H:%M')} MST live snapshot",
            "**Posture:** custodes daily-note surface",
            f"**Balance:** {balance} · timer mode: {str(mode).upper()}",
            f"**Active:** {active}",
            f"**Geofence:** {location} · desktop_mode: {desktop}",
            f"**Cascade:** {cascade}",
            "",
            f"*Last updated {now.strftime('%H:%M')} MST*",
        ]
    )


def _derive_timer_mode(timer: dict) -> str:
    manual_mode = timer.get("manual_mode")
    if manual_mode:
        return str(manual_mode)
    activity = timer.get("activity")
    productivity_active = bool(timer.get("productivity_active"))
    if activity == "distraction":
        return "multitasking" if productivity_active else "break"
    if activity == "working":
        return "working" if productivity_active else "idle"
    return "unknown"


def compose(db_path: str | Path, *, now: datetime | None = None) -> str:
    return compose_now_markdown(load_telemetry(db_path), now=now)


def write_today_now_callout(
    db_path: str | Path,
    daily_note_dir: str | Path,
    *,
    today: datetime | None = None,
) -> CalloutWriteResult:
    today_dt = today.astimezone(MST) if today else datetime.now(MST)
    note_path = Path(daily_note_dir) / f"{today_dt.strftime('%Y-%m-%d')}.md"
    content = compose(db_path, now=today_dt)
    return apply_callout(note_path, "now", content, title="NOW", callout_type="info")
