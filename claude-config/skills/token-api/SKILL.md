---
name: token-api
description: Token-API service shorthand. Use when inspecting or working with the local FastAPI coordination service, health, OpenAPI schema, instance/session endpoints, restart/status commands, logs, or service paths.
---

# Token-API

Token-API is the local FastAPI coordination service for instance registry, session docs, notifications, enforcement, timers, Discord ingest, and ops read models.

## Surfaces

- Service URL: `$TOKEN_API_URL` from `nas-path.sh` / `imperium_config.py` (`http://localhost:7777` on Mac; Mac Tailscale URL elsewhere).
- Runtime checkout: `${TOKEN_OS:-$HOME/runtimes/Token-OS/live}`.
- Service code: `token-api/`; logs: `~/.claude/token-api-stdout.log`, `~/.claude/token-api-stderr.log`.
- CLI: `token-ping`, `token-status`, `token-restart`.
- Health/schema: `GET $TOKEN_API_URL/health`, `GET $TOKEN_API_URL/openapi.json`, `GET $TOKEN_API_URL/docs`.
- DB paths: `TOKEN_API_AGENTS_DB`, `TOKEN_API_TIMER_DB`, defaulting under `~/runtimes/database`.

## Safe checks

```bash
source "${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/cli-tools/lib/nas-path.sh"
token-ping --raw /health
curl -sf "$TOKEN_API_URL/openapi.json" | jq '.openapi'
token-status
```

## Do Not

- Do not use `/api/schema` or `/api/openapi.json`; the current OpenAPI schema is `/openapi.json`.
- Do not hardcode Tailscale IPs, NAS paths, or localhost; use `$TOKEN_API_URL`, `$TOKEN_OS`, `$IMPERIUM`, or `imperium_cfg`.
- Do not patch SQLite directly when a Token-API endpoint or sanctioned helper exists.
- Do not restart live Token-API just to test syntax.
