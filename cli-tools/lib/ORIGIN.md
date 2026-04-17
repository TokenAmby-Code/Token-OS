# origin — I-side primitive

`nas-path.sh` answers **"what is machine X?"** — a static registry of known machines (O-side: where to send, how to reach, what's my identity).

`origin.sh` answers **"who is invoking this?"** — a dynamic, per-invocation resolver (I-side: who pressed the key, who sent the request, which pane owns this process).

Both primitives share a naming convention and a first-class place in `shell-init.sh`. Neither leaks into the other.

## Record shape

```
{
  machine          mac | wsl | phone | linux | unknown
  device_id        canonical device name (Mac-Mini, TokenPC, Token-S24)
  client_pid       tmux client PID (when applicable)
  pane             tmux pane id (%N) or @PANE_ID
  instance_id      claude_instances.id (when applicable)
  session_doc_id   session_documents.id (when applicable)
  geofence         home | away | unknown
  transport        tmux | ssh | http | cron | local
}
```

Not every slot is populated for every invocation. Resolvers fill what they can. Callers take what they need.

## Override hierarchy

Every resolver checks in this order:

1. **Env var override** — `IMPERIUM_ORIGIN_<SLOT>` (e.g., `IMPERIUM_ORIGIN_MACHINE=wsl`). Highest precedence. Sidesteps all resolution. Use for testing, forced routing, or bypassing the wrapper dependency.
2. **Cache file** — `${TMPDIR:-/tmp}/imperium-origin-<client_pid>.<slot>`. Populated on first resolve, reused for subsequent calls in the same client lifetime. Best-effort; safe to delete.
3. **Live resolution** — transport-specific (tmux → `client_pid` walk, HTTP → peer IP + `device_id` header, etc.). Expensive; result is cached.

## Resolvers (today)

| Resolver | Status | Implementation |
|---|---|---|
| `origin_machine` | shipped | client_pid → sshd ancestor → peer IP → `imperium_cfg tailscale_ip` lookup. Mac uses `lsof`, Linux reads `/proc/<pid>/environ`. Falls back to `$IMPERIUM_MACHINE` when no tmux context. |
| `origin_pane` | shipped | `TMUX_PANE` env var or `#{pane_id}` from tmux. |
| `origin_device_id` | shipped | Derives from `origin_machine` via `imperium_cfg device_name`. |
| `origin_instance` | stub | TODO: call `/api/instances/resolve` via pane + pid. |
| `origin_geofence` | stub | TODO: call Token-API geofence endpoint. |
| `origin_record` | shipped | Prints all resolved slots as JSON. |

## Adding a new resolver

1. Name it `origin_<slot>` — lowercase, underscore-separated.
2. Check override env var `IMPERIUM_ORIGIN_<SLOT>` first.
3. Check `${TMPDIR:-/tmp}/imperium-origin-<client_pid>.<slot>` cache.
4. Do the live resolution.
5. Write to the cache file.
6. Echo the result on stdout. No other output.
7. Register in `origin_record`.
8. Add a test case to `cli-tools/tests/test-origin.sh`.

## HTTP-side counterpart (planned)

`token-api/origin.py` will provide the same record shape via FastAPI middleware. Request headers carry `X-Origin-Machine`, `X-Origin-Pane`, `X-Origin-Device`; middleware resolves the rest from peer IP, `device_id` header, and existing instance-resolver logic. Until it exists, HTTP handlers that need origin info use the shell-side `origin_machine` over a subprocess — slower but consistent.

## Why not just extend `imperium_cfg`?

`imperium_cfg` is a pure lookup on a compile-time registry — no side effects, no caching, no resolution. `origin_*` resolves state that only exists at invocation time (which tmux client, which TCP peer, which pane). Mixing them would couple dynamic resolution to static config and make both harder to test.

The rule:
- Static fact about a known machine → `imperium_cfg`.
- Dynamic fact about the current invocation → `origin_*`.
