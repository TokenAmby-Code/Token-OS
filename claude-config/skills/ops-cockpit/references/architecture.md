# Ops Cockpit Architecture Reference

The ops cockpit is a Vite/React/TypeScript frontend under `token-api/web/ops/` with a committed static build under `token-api/ui/ops/`. FastAPI serves the built UI and the aggregate state endpoint.

## Contract Shape

- Prefer one aggregate read model for dashboard load: `/api/ui/ops/state`.
- Keep TypeScript types close to the API contract.
- Components should consume typed selectors/adapters, not fetch arbitrary Token-API routes independently.
- Controls should call explicit Token-API endpoints; Token-API performs registry/session mutation or delegates tmux actions to tmuxctld.

## Terminus Role

This is the migration anchor for Token-OS web-native operator surfaces. It should demonstrate shared stack muscle memory with askCivic without copying askCivic domain code, data, prompts, or business logic.

## Validation Matrix

- Typecheck the frontend.
- Build and verify committed output when required by the repo workflow.
- Hit `/api/ui/ops/state` and inspect degraded/healthy status.
- Browser-test against same-host Token-API where possible.
