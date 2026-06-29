# generic-token-api-durable-retry-outbox

Durable client-side retry/replay outbox for hook → Token-API POSTs that fail because the service is unreachable (`http=000` / connection refused).

Compatibility alias: `hook-token-api-queue` (old ticket slug:
`hook-token-api-retry-queue`).

## Library survey / chosen approach

Surveyed options:

- `persist-queue`: maintained persistent Python queue package with SQLite/file backends; good FIFO primitive, but it would add a package/bootstrap dependency to stripped Claude hook shells and does not directly provide the outbox status/idempotency/audit model we need.
- `aiodiskqueue`: async disk queue primitive; good for Python async workers, less useful for shell hook call sites and still another dependency.
- Huey SQLite storage: mature task queue, but it brings a worker/consumer model. W2 explicitly drains on the existing Token-API heartbeat down→up edge, not a resident consumer or blind poll.

Chosen: stdlib SQLite outbox in `cli-tools/bin/generic-token-api-durable-retry-outbox`.

Rationale: no new runtime dependency in hook environments, durable across restarts, SQLite locking/WAL provide serialized concurrent enqueue safety, and the schema directly represents outbox needs: ordered rows, idempotency key, attempts, status, HTTP outcome, and audit log.

## Storage

Default DB: `~/.claude/generic-token-api-durable-retry-outbox.sqlite3`

Default log: `~/.claude/logs/generic-token-api-durable-retry-outbox.log`

Schema table: `hook_posts` ordered by autoincrement `id`.

Idempotency key:

1. `SessionStart`/`SessionEnd`: `<action_type>:session:<session_id>`
2. `WrapperStart`/`WrapperEnd`: `<action_type>:wrapper:<wrapper_launch_id>`
   or `<action_type>:wrapper:<env.TOKEN_API_WRAPPER_LAUNCH_ID>`
3. Repeatable hook actions (`PreToolUse`, `UserPromptSubmit`, etc.):
   `<action_type>:sha256:<payload>`

## Drain trigger

`cli-tools/Shell/tokenapi-watchdog` drains only when its heartbeat state transitions from `down` to `up`; it does not poll the outbox while healthy.

## Scope

In scope: hook → Token-API POSTs from:

- `claude-config/hooks/generic-hook.sh`: `SessionStart`, `PreToolUse`, and all other hook action types.
- `cli-tools/lib/agent-wrapper-common.sh`: wrapper lifecycle hook POSTs (`WrapperStart`, `WrapperEnd`, etc.).

Only `http=000`/connection-refused unreachable class is enqueued. HTTP non-2xx is marked failed on replay and remains loud. Pre-POST `http=?` shell aborts are not handled here.

## tmuxctld

Deferred. `WrapperEnd` also posts to tmuxctld, but a correct replay trigger needs a tmuxctld recovery edge/heartbeat. Reusing the Token-API heartbeat edge would either be a blind retry for tmuxctld or could replay while tmuxctld remains down. The wrapper code now has a clean seam (`token_wrapper_enqueue_hook_post`) for Token-API; a sibling tmuxctld outbox should be added with a tmuxctld watchdog recovery trigger if needed.
