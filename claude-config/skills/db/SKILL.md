---
name: db
description: Token-OS SQLite database orientation. Use when inspecting agents.db, timer.db, live instance state, session-doc links, dispatcher registry behavior, or DB paths; defaults to read-only and warns against self-patching identity rows.
---

# DB

Token-OS SQLite state defaults to `~/runtimes/database` on the Mac host. Worktrees and services may override it. Do not hardcode database paths; resolve them through the machine config helpers or service env.

## Files

- `agents.db` — primary registry for live agents, instances, session docs, dispatch state, and related control tables.
- `timer.db` — high-spam timer/telemetry data. Query only when that stream is relevant.

Use the configured path. Shell should source `nas-path.sh`; Python should import `imperium_config.py` and read `TOKEN_API_AGENTS_DB` / `TOKEN_API_TIMER_DB`.

## Primary Table

`instances` is the primary live-agent table. It is the first place to inspect pane identity, instance type, persona/legion, session-doc binding, and recent activity.

Typical read-only probes:

```bash
source "${TOKEN_OS_ROOT:-${TOKEN_OS:-$HOME/runtimes/Token-OS/live}}/cli-tools/lib/nas-path.sh" 2>/dev/null || true
DB_ROOT="${TOKEN_API_DATABASE_DIR:-$HOME/runtimes/database}"
AGENTS_DB="${TOKEN_API_AGENTS_DB:-${TOKEN_API_DB:-$DB_ROOT/agents.db}}"
TIMER_DB="${TOKEN_API_TIMER_DB:-$DB_ROOT/timer.db}"

sqlite3 "$AGENTS_DB" '.tables'
sqlite3 "$AGENTS_DB" 'PRAGMA table_info(instances);'
sqlite3 "$AGENTS_DB" "SELECT id, name, legion, primarch, instance_type, session_doc_id, updated_at FROM instances ORDER BY updated_at DESC LIMIT 20;"
sqlite3 "$TIMER_DB" '.tables'
```

Python path resolution:

```python
from imperium_config import TOKEN_API_AGENTS_DB, TOKEN_API_TIMER_DB
```

Prefer Token-API for live behavior when an endpoint exists:

```bash
token-ping instances/resolve pid=$PID cwd=$(pwd)
curl -s "$TOKEN_API_URL/api/instances?sort=recent_activity" | jq '.[0:10]'
```

## Rules

- Default to read-only queries.
- Do not self-register, self-correct identity, or PATCH your own DB row. A wrong stamp is a harness/registry bug to report.
- Do not write `agents.db` directly unless the task explicitly assigns DB migration/repair and you have a backup/rollback plan.
- Treat `timer.db` as noisy; filter narrowly.
