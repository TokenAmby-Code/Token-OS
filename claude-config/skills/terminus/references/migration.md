# Terminus Migration Reference

Terminus is direction, not rewrite license.

## Current Anchor

The ops cockpit is the current Token-OS migration nexus: a Vite/React/TypeScript frontend served by Token-API with typed aggregate state and operator controls. New dashboard/operator work should extend this pilot unless there is a concrete reason not to.

## Boundary

- Token-OS: local orchestration, tmux, hooks, enforcement, TTS, phone/desktop adapters, operator cockpit.
- askCivic: procurement workflows, RAG, civic domain data, public/product surfaces.
- Shared: TypeScript UI/API contracts, CI/CD discipline, component patterns, health/smoke validation habits.
- Not shared: business logic, domain data, prompts, customer artifacts, or production credentials.

## Python Retirement Rule

Retire Python paths only when a typed web/service replacement exists, has tests, and preserves local side effects. Do not delete working runtime adapters merely because a web surface now observes or triggers them.
