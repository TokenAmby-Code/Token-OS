# OpenClaw Cron Jobs

Full reference for managing scheduled agent tasks via the OpenClaw cron system.

## Job Structure

Jobs are stored in `~/.openclaw/cron/jobs.json`. Each job has:

```json
{
  "name": "task-worker",
  "description": "Picks tasks and does one step per run",
  "enabled": true,
  "schedule": {
    "kind": "every",          // "every" or "cron"
    "everyMs": 7200000        // for kind=every: interval in ms (2h)
    // "cron": "0 9 * * 1-5"  // for kind=cron: standard cron expression
  },
  "sessionTarget": "isolated",  // "isolated" = fresh session each run
  // or: "named-session-id"     // reuse a named session (persistent context)
  "payload": {
    "kind": "agentTurn",
    "message": "Your prompt here...",
    "thinking": "low",          // "none", "low", "medium", "high"
    "timeoutSeconds": 240       // max execution time
  },
  "delivery": {
    "mode": "none",             // "none" = silent, "announce" = post result
    "channel": "discord",       // delivery channel (when mode=announce)
    "to": "channel-id"          // target channel/user ID
  }
}
```

## Schedule Types

### `every` (interval)
Runs at a fixed interval from an anchor time.

```json
{ "kind": "every", "everyMs": 3600000 }     // every 1 hour
{ "kind": "every", "everyMs": 7200000 }     // every 2 hours
{ "kind": "every", "everyMs": 86400000 }    // every 24 hours
```

### `cron` (cron expression)
Standard 5-field cron syntax.

```json
{ "kind": "cron", "cron": "0 9 * * 1-5" }   // 9 AM weekdays
{ "kind": "cron", "cron": "*/30 * * * *" }   // every 30 minutes
{ "kind": "cron", "cron": "0 21 * * *" }     // 9 PM daily
```

## Delivery Modes

| Mode | Behavior |
|------|----------|
| `none` | Job runs silently, output only in run history |
| `announce` | Posts the agent's response to the specified channel |

When `mode` is `announce`, set `channel` (e.g. `"discord"`) and `to` (channel/user ID).

## Commands

### `openclaw cron list`
List all configured cron jobs with status and schedule.

```bash
openclaw cron list
```

### `openclaw cron add`
Add a new cron job interactively.

```bash
openclaw cron add
```

### `openclaw cron edit`
Patch fields on an existing job (name, schedule, prompt, etc.).

```bash
openclaw cron edit
```

### `openclaw cron rm`
Remove a cron job permanently.

```bash
openclaw cron rm
```

### `openclaw cron enable`
Enable a disabled cron job.

```bash
openclaw cron enable
```

### `openclaw cron disable`
Disable a cron job (keeps config, stops scheduling).

```bash
openclaw cron disable
```

### `openclaw cron run <name>`
Run a cron job immediately (for testing/debugging). Does not affect schedule.

```bash
openclaw cron run task-worker
```

### `openclaw cron runs`
Show run history from JSONL logs.

```bash
openclaw cron runs
```

### `openclaw cron status`
Show scheduler status: next run times, enabled jobs, error counts.

```bash
openclaw cron status
```

## Session Targeting

| Target | Behavior |
|--------|----------|
| `"isolated"` | Fresh session each run (no memory between runs) |
| `"my-session-id"` | Reuses a named session (persistent context across runs) |

Use `isolated` for stateless tasks (security scans, reports). Use named sessions when the agent needs to remember prior runs (multi-step workflows).

## Payload Options

| Field | Values | Default |
|-------|--------|---------|
| `thinking` | `"none"`, `"low"`, `"medium"`, `"high"` | `"low"` |
| `timeoutSeconds` | 30-600 | 120 |
| `message` | The agent prompt | (required) |

Higher thinking levels use more tokens but improve reasoning. Keep `timeoutSeconds` generous for complex tasks.

## Tips

- **Test before scheduling**: Use `openclaw cron run <name>` to verify a job works before enabling it
- **Workspace context**: All cron agents share the workspace at `~/.openclaw/workspace/`. For project-specific context, include `cat context/<project>/AGENTS.md` in the prompt
- **Claude delegation**: For coding tasks, have the cron agent delegate to Claude Code: `claude -p "task description"` in exec mode
- **Keep prompts focused**: Each run should do ONE concrete step. Small, self-contained actions are more reliable
- **Delivery for visibility**: Use `announce` mode to get Discord notifications of job results
- **Error handling**: Check `openclaw cron status` for `consecutiveErrors` — jobs with repeated failures may need prompt fixes
- **Backup jobs.json**: Before bulk edits: `cp ~/.openclaw/cron/jobs.json ~/.openclaw/cron/jobs.json.bak`
