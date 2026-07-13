# edge_proxy

Stateless Bun/TypeScript routing proxy for per-box Token-OS ingress.

Day-one scope:
- `/health` reports proxy liveness, build identity, and per-route upstream `/health` reachability.
- Per-route allowlisted forwarding from localhost consumers to the route's upstream.
- Stop-hook/report forwarding via allowlisted `/api/hooks/` paths.

Invariants (spec §12 — one edge proxy per box, the box's front door):
- The proxy stays **DUMB**: routing, auth, and admission (allowlist) only. Code
  enters the proxy only if it must run *before* routing resolves. No per-upstream
  business logic accreting at the front door.
- **Route-scoped auth**: a route's optional bearer `token` gates only that route —
  compromising one upstream's cred never grants another route or the box. The
  proxy terminates the cred and never forwards `Authorization` upstream. A
  **tokenless** route passes the caller's `Authorization` through untouched:
  upstreams that do their own bearer auth (e.g. token-api `/api/cd/restart`,
  fail-closed on `CD_RESTART_SECRET`) remain the auth authority.
- No queues, retry buffers, store-and-forward, or dedupe caches.
- Upstream failure returns an immediate `502 upstream_unreachable` and logs the error.
- Bind, port, machine, and the route table (prefix → upstream, allowlist, token)
  are config/env controlled.
- Bun-native: run TypeScript source directly; install only with `bun install --frozen-lockfile` after `bun.lock` is committed.

## Routing

Requests are matched to a route by **longest path prefix** (`/` is the catch-all).
A route may `stripPrefix` so the upstream sees its own paths. Example: the k12
daemon lives behind `/k12` and is reached as `/k12/health → 127.0.0.1:7781 /health`,
while everything else falls through to Token-API on `:7777`. Cross-box traffic is
proxy-to-proxy over the tailnet — each box has exactly one front door.

The `/token-api` route exists because the proxy answers `GET /health` itself
(its own liveness), which shadows the upstream's. CD callers reach token-api's
`/health` deploy proof as `/token-api/health` and the merge webhook as
`/token-api/api/cd/restart` — one tightly-allowlisted route for the box's CD
ingress, keeping token-api loopback-bound (one ingress per box: the proxy binds
the tailnet IP; nothing else does).

## Config

Set `EDGE_PROXY_CONFIG` to a JSON file matching `edge_proxy.config.example.json`
(a `routes` array), or use env defaults. The legacy single-`upstream` shape is
still accepted and folds into one `/` route.

- `EDGE_PROXY_BIND` default `127.0.0.1`
- `EDGE_PROXY_PORT` default `7780`
- `EDGE_PROXY_UPSTREAM` default `$TOKEN_API_URL` then `http://127.0.0.1:7777` (default `/` route only)
- `IMPERIUM_MACHINE` default `auto`

## Development

```bash
cd edge_proxy
bun install --frozen-lockfile
bun test
bun src/server.ts
```

No build step.
