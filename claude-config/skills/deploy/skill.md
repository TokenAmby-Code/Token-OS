---
name: deploy
description: "Deploy services to Cloud Run. Usage: /deploy [service] [env]. Services: backend (default), web, widget-proxy, chat-proxy, proxies. Envs: dev (default), prod."
user_invocable: true
---

# Deploy

Deploy askCivic services via GitHub Actions CI/CD pipeline.

## Usage

- `/deploy` — deploy backend to dev
- `/deploy backend` — deploy backend to dev
- `/deploy backend prod` — deploy backend to production
- `/deploy web` — deploy frontend widget to dev
- `/deploy web prod` — deploy frontend to production
- `/deploy widget-proxy` — deploy widget proxy to dev
- `/deploy chat-proxy` — deploy chat proxy to dev
- `/deploy proxies` — deploy both proxies to dev
- `/deploy proxies prod` — deploy both proxies to production

## Process

1. Parse the service and environment from the user's arguments (default: `backend dev`)
2. Map the service name to the GitHub Actions `services` input value:
   - `backend` → `backend`
   - `web` / `frontend` → `frontend`
   - `widget-proxy` / `chat-proxy` / `proxies` → `proxy`
   - `all` → `all`
3. Map the environment:
   - `dev` / `development` (default) → `development`
   - `prod` / `production` → `production`
4. Trigger the deploy:

```bash
gh workflow run deploy.yml \
  -f environment=<environment> \
  -f services=<services> \
  --repo ColbyLanier/ProcurementAgentAI
```

5. Watch the deploy progress:

```bash
# Get the run ID (most recent workflow_dispatch run)
gh run list --repo ColbyLanier/ProcurementAgentAI --limit 3

# Watch it
gh run watch <run-id> --repo ColbyLanier/ProcurementAgentAI
```

## How the Pipeline Works

- **On push to `main`**: Auto-detects changed paths and deploys only affected services to dev
- **On `workflow_dispatch`**: Deploys selected services to the chosen environment
- **Authentication**: Workload Identity Federation (keyless) — auto-selects dev or prod SA
- **Backend**: Docker build → GCR push → Cloud Run service replace (YAML config) → health check
- **Frontend**: npm ci → Vite build → GCS upload (cache-optimized) → CDN invalidation → smoke test
- **Proxies**: Docker build → GCR push → Cloud Run service replace → public IAM binding

## Services Reference

| Service | Dockerfile | Config YAML pattern |
|---------|-----------|-------------------|
| backend | `deploy/Dockerfile` | `deploy/pax-{environment}.yaml` |
| web | N/A (Vite build) | N/A (GCS bucket upload) |
| widget-proxy | `deploy/Dockerfile.proxy` | `deploy/pax-widget-proxy-{environment}.yaml` |
| chat-proxy | `deploy/Dockerfile.proxy` | `deploy/pax-google-chat-proxy-{environment}.yaml` |

## After Deploy

Report the result: which service was deployed, to which environment. If the deploy fails, show the failed step logs:

```bash
gh run view <run-id> --repo ColbyLanier/ProcurementAgentAI --log-failed
```
