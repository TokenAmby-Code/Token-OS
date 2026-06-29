---
name: ops-cockpit
description: "Token-OS ops cockpit work: /ui/ops Vite/React/TypeScript frontend, Token-API aggregate read-model endpoints, dashboard state contracts, typed operator controls, Terminus pilot surface, and live operator observation/verification."
---

# Ops Cockpit

The ops cockpit is the current Token-OS Terminus demo surface and live operator dashboard served by Token-API. Extend it rather than creating parallel dashboards. For architecture detail, read `references/architecture.md`.

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

## Doctrine

- Observation-first: degraded real state is better than fake state.
- Token-API-routed controls are allowed when they preserve service boundaries and typed contracts.
- Keep nested components using centralized typed data access instead of stitching legacy endpoints directly.

## Do Not

- Do not build a parallel live dashboard, static Obsidian replacement, or alternate state stitcher.
- Do not fake live timer, instance, dispatch, or health data on the operator surface.
- Do not run browser automation against the physical Windows vertical-monitor cockpit unless physical display behavior is the task.
