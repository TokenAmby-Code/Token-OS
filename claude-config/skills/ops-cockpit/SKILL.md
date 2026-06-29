---
name: ops-cockpit
description: Ops cockpit shorthand. Use when working on the Token-API Terminus web cockpit, /ui/ops frontend, aggregate read-model endpoints, dashboard state contracts, or live operator surface.
---

# Ops Cockpit

The ops cockpit is the live Token-OS dashboard surface served by Token-API. Extend this cockpit rather than creating parallel live dashboards.

## Surfaces

- Browser: `$TOKEN_API_URL/ui/ops`.
- Aggregate state: `GET $TOKEN_API_URL/api/ui/ops/state`.
- Frontend source: `token-api/web/ops/` (Vite + React + TypeScript).
- Committed build: `token-api/ui/ops/` served by FastAPI.
- Design/reference: `token-api/docs/ops-cockpit.md`, `token-api/docs/ops-cockpit-frontend-design-brief.md`.

## Validation

```bash
cd token-api/web/ops
npm run typecheck
npm run build
curl -sf "$TOKEN_API_URL/api/ui/ops/state" | jq .surface
```

## Do Not

- Do not build a parallel live dashboard, static Obsidian replacement, or alternate state stitcher.
- Do not have nested components call legacy endpoints directly; keep typed data access centralized.
- Do not fake live timer data; degraded state is better than mock state on the operator surface.
- Do not run browser automation against the physical Windows vertical-monitor cockpit; use same-host localhost Mac cockpit for tests.
