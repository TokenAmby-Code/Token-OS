# civic-invariant — one-civic-background-invariant harness

Keeps **exactly one healthy askCivic instance alive** in the background, and lets
other civic work assert that before it starts.

> "I need one civic instance running before I can work on other stuff."

## TL;DR for the Emperor

```bash
civic-invariant status      # is one civic instance alive? (human readable)
civic-invariant check       # quiet; exit 0 iff satisfied (use in scripts)
civic-invariant ensure      # make it so (idempotent: start/respawn proxy + app)
civic-invariant require     # what civic work runs FIRST: ensure + confirm alive
civic-invariant restart     # stop + ensure
civic-invariant stop        # stop app  (add --proxy to also stop the proxy)
civic-invariant logs        # tail instance log (--proxy for proxy log)
```

`civic-invariant` is on PATH (symlink in `/opt/homebrew/bin`). Source lives here in
`~/.civic-invariant/`.

## What "alive" means

The invariant is satisfied when **all** hold:
- the Cloud SQL Auth Proxy is listening on `127.0.0.1:5432`, AND
- exactly one askCivic backend owns `:8080` and `GET /health` returns **HTTP 200**.

Liveness is the **health HTTP response + port ownership**, never bare process
existence (the uvicorn reloader can outlive a dead worker). The OS guarantees at
most one listener on `:8080` — that is what enforces "exactly one". `ensure` also
reaps orphan app processes that aren't the port owner.

## Pieces

| File | Role |
|---|---|
| `civic-invariant` | control script (status/check/ensure/require/restart/stop/logs) |
| `server.py` | single-process, no-reload uvicorn launcher (clean PID, absolute creds) |
| `com.civic.invariant.plist` | launchd keeper template — **staged, not loaded** |
| `instance.log` / `proxy.log` | runtime logs of the app / proxy |
| `harness.log` | harness events (starts, reaps, respawns, errors) |
| `instance.pid` / `proxy.pid` | last-launched PIDs (advisory; port is source of truth) |

## Two DB connection paths (why the proxy exists)

askCivic talks to dev Cloud SQL (`pax-dev-469018:us-central1:pax-sql`) two ways:
1. **Main app pool** → Cloud SQL **Python Connector** (uses `INSTANCE_CONNECTION_NAME`
   over :443 + the SA key). Works from anywhere; ignores `DB_HOST`.
2. **LangGraph checkpointer** (`app/utils/checkpoint_factory.py`) → **direct psycopg
   DSN** to `DB_HOST:5432`. This Mac's public IP is **not allowlisted** on the dev
   SQL public IP, so the direct path times out and **fails app startup**.

Fix used: run a local **cloud-sql-proxy** on `127.0.0.1:5432` (auth via the worktree
SA key `deploy/dev-service-account.json`, no interactive ADC login) and set
`DB_HOST=127.0.0.1` in the worktree `.env`. The checkpointer then tunnels through
the proxy; the main pool is unaffected. The harness supervises the proxy too.

## Mount note

Worktree, `.venv`, proxy binary and SA key are all on **local disk**, so keep-alive
and respawn work even when the Civic AES-256 mount is **locked**. Only a from-scratch
rebuild (`worktree-setup`) needs Civic unlocked. If the worktree/.venv/SA-key are
missing, `ensure` logs an error and exits non-zero (no thrash).

## Keeper (always-on) — needs an Emperor decision

`ensure` is idempotent but one-shot. Something must call it on a loop. Options:
- **launchd** (provided): `com.civic.invariant.plist`, `RunAtLoad` + `StartInterval=60`.
  Enable: `cp com.civic.invariant.plist ~/Library/LaunchAgents/ && launchctl load -w ~/Library/LaunchAgents/com.civic.invariant.plist`
- **Mechanicus fleet cron**: a job running `civic-invariant ensure` each minute.
- **dispatch guard**: civic launches call `civic-invariant require` before work.

Not enabled automatically — pick one.
