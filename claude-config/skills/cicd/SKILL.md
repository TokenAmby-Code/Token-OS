---
name: cicd
description: CI/CD troubleshooting and deployment-path routing for Token-OS and askCivic. Use instead of deploy when checking local sync/restart, GitHub Actions, Workload Identity Federation, Cloud Run, GCS widget frontend, dev.askcivic.com smoke checks, or deployment failures.
---

# CI/CD

Use this skill to diagnose or route CI/CD work. Read the project-specific reference before acting:

- Token-OS / Token-API / local hot runtime: read `references/token-os.md`.
- askCivic / GitHub Actions / Cloud Run / GCS widget: read `references/askcivic.md`.

## Universal Rules

- Fix the root cause; do not bypass the pipeline because CI is red.
- Prefer evidence: run IDs, SHAs, health endpoints, smoke checks, logs, and exact failure steps.
- Do not invent production deploy commands. If the reference does not name a deploy path, report that no safe path is documented.
- Use machine config (`$TOKEN_API_URL`, `$IMPERIUM`, `$CIVIC`) rather than hardcoded paths or IPs.

## Safe First Checks

```bash
git status --short --branch
curl -sf "$TOKEN_API_URL/health"
```
