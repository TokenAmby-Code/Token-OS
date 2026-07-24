# Imperium daily-note write control

`TOKEN_API_DISABLE_AUTOMATIC_IMPERIUM_DAILY_NOTE_WRITES=1` disables Token-API
automation that can mutate `Terra/Journal/Daily/**`. The Mac LaunchAgent
`ai.openclaw.tokenapi` sets this control. Other hosts do not set it, so their
sanctioned ingress is unchanged.

The disabled automatic paths are:

- 06:00 and startup-recovery Custodes lifecycle prompt injection;
- prior-day timer analytics JSON, SVG, frontmatter, and callout writes;
- day-start daily-note creation and singleton session-document rebind;
- Hatch alarm-silenced and schedule-fallback morning-session launch;
- `/api/morning/start` and `/api/custodes/morning-brief` launch/injection;
- `morning_session.py` note creation, thread-id write, and Custodes launch;
- SessionStart persona-default Imperium daily-note creation/binding;
- morning Stop-hook keepalive injection.

The 06:00 timer transition, daily reset, day-state latch, quiet-hours state,
phone reachability, enforcement, scheduler, database state, Token-API, tmuxctld,
the Custodes seat, Obsidian, and Obsidian Sync remain active. `/health` reports
the control state.

The Mac Stop hook retains transcripts in Imperium-Logs but never appends their
links to an Imperium daily note.

## Explicit writer surfaces

These paths remain available only through explicit operator or user actions and
are not called by the disabled morning pipeline:

- `POST /api/daily-note/append`;
- `PUT /api/daily-note/callout`;
- check-in submission frontmatter updates;
- explicit `/api/work-action` button presses;
- direct execution of `custodes_checkin.py` or `custodes_heartbeat.py`;
- session-document helper calls made outside the guarded automatic
  persona/day-start routes.

The permanent-job rows for the standalone Custodes check-in/heartbeat programs
are disabled on the Mac. The enabled `day_start_schedule_fallback` job remains
enabled because its unrelated fanout still runs; its three note/morning
consumers report `skipped` while the service control is active.
