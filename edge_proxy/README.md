# edge_proxy

Stateless Bun/TypeScript routing proxy for per-box Token-OS ingress.

Day-one scope:
- `/health` reports proxy liveness, build identity, and upstream `/health` reachability.
- Generic allowlisted forwarding from localhost consumers to configured Token-API upstream.
- Stop-hook/report forwarding via allowlisted `/api/hooks/` paths.

Invariants:
- No queues, retry buffers, store-and-forward, or dedupe caches.
- Upstream failure returns an immediate `502 upstream_unreachable` and logs the error.
- Bind, port, upstream, machine, and route allowlist are config/env controlled.
- Bun-native: run TypeScript source directly; install only with `bun install --frozen-lockfile` after `bun.lock` is committed.

## Config

Set `EDGE_PROXY_CONFIG` to a JSON file matching `edge_proxy.config.example.json`, or use env defaults:

- `EDGE_PROXY_BIND` default `127.0.0.1`
- `EDGE_PROXY_PORT` default `7780`
- `EDGE_PROXY_UPSTREAM` default `$TOKEN_API_URL` then `http://127.0.0.1:7777`
- `IMPERIUM_MACHINE` default `auto`

## Development

```bash
cd edge_proxy
bun install --frozen-lockfile
bun test
bun src/server.ts
```

No build step.
