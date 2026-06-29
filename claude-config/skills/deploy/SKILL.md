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

## CRITICAL: CI/CD Pipeline is the ONLY Deploy Path

**NEVER deploy manually with gsutil, gcloud storage, or raw shell commands.** All deploys MUST go through the GitHub Actions CI/CD pipeline. This is non-negotiable.

**If CI fails, fix the root cause and re-trigger CI.** Common CI failures and their fixes:
- **GCS 403 / permission denied**: IAM issue — grant the SA the needed role, then re-trigger CI
- **Build failure**: Fix the code, push, CI re-runs automatically on push to main
- **Smoke test failure**: The smoke test caught a real problem — fix it before deploying

**Why this rule exists:** On 2026-04-08, a manual deploy bypassed CI's build scripts and smoke tests, shipping a broken frontend (missing Clerk auth key = white screen on prod). The CI pipeline has guardrails (dotenv-cli build, API URL verification, Clerk key smoke test) that manual deploys skip entirely.

**If you are tempted to run gsutil or gcloud storage commands to upload frontend assets, STOP.** Re-read this section. Fix the CI issue. Re-trigger CI.

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

6. **If the deploy fails**: Get the failed step logs, diagnose, fix the underlying issue, and re-trigger CI:

```bash
gh run view <run-id> --repo ColbyLanier/ProcurementAgentAI --log-failed
# Fix the issue, then re-trigger:
gh workflow run deploy.yml -f environment=<env> -f services=<svc> --repo ColbyLanier/ProcurementAgentAI
```

## GCS Bucket Access

The prod SA (`pax-service-account@pax-prod-467920.iam.gserviceaccount.com`) has `objectAdmin` on `gs://askcivic-site` (which lives in the dev project `pax-dev-469018`). This same SA is used by CI via Workload Identity Federation AND exists as a local key in `askcivic.secrets/deploy/prod-service-account.json`.

**The local key file exists for backend/proxy deploys and debugging — NOT for manual frontend uploads.** IAM cannot distinguish CI-via-WIF from local-key usage for the same SA. The guardrail is behavioral: this skill, the verify_build.sh script, and the CI smoke test.

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

Report the result: which service was deployed, to which environment. The CI pipeline runs a smoke test automatically — if it passes, the deploy is verified.

If the deploy **fails**, show the failed step logs, diagnose, and re-trigger:

```bash
gh run view <run-id> --repo ColbyLanier/ProcurementAgentAI --log-failed
```

## Troubleshooting: CI Failures

| Symptom | Root Cause | Fix |
|---------|-----------|-----|
| GCS 403 on upload | SA lacks bucket IAM | Grant `objectAdmin` on bucket, re-trigger CI |
| "Bundle does not contain expected API URL" | Wrong env file or build mode | Check `.env.production` / `.env.development` in widget/ |
| Smoke test: Clerk key not found | Build ran without dotenv-cli | CI handles this — if it fails, check the env file content |
| CDN invalidation 403 | SA lacks compute permissions | Grant on CDN project (`pax-dev-469018`), re-trigger CI |
