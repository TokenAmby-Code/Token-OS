# agents-db

Query the local instance registry database (`~/.claude/agents.db`, SQLite).

## Commands

```bash
agents-db tables                      # List all tables
agents-db describe <table>            # Show table schema
agents-db query "SELECT ..."          # Run SQL query (auto-limits to 100)
agents-db instances                   # Active instances with status
agents-db events --limit 20           # Recent events
```

## Options

| Option | Description |
|--------|-------------|
| `--json` | JSON output (works with all commands) |
| `--limit N` | Limit results (events command) |

## Key Tables

| Table | Contents |
|-------|----------|
| `claude_instances` | Active instances: id, tab_name, working_dir, status, device_id, last_activity |
| `events` | Event log: event_type, instance_id, details, created_at |

## Examples

```bash
# Check what instances are running
agents-db instances

# Get events for debugging
agents-db events --limit 50

# Custom query
agents-db query "SELECT tab_name, status FROM claude_instances WHERE status='active'"

# JSON output for scripting
agents-db instances --json
```

## Features

- **Auto-limiting**: SELECT queries without LIMIT get `LIMIT 100`
- **JSON parsing**: Automatic parsing of JSON fields with `--json`
- **Smart output**: Aligned columns, truncated at 40 chars
