# askCivic CI/CD Reference

askCivic deploys through GitHub Actions using Workload Identity Federation. The current repository is `PubKnow-Civic/askCivic`.

## Surfaces

- CI/CD: GitHub Actions in `PubKnow-Civic/askCivic`.
- Backend/proxies: Cloud Run.
- Widget/frontend: Vite build uploaded to GCS-backed site/CDN.
- Dev site: `https://dev.askcivic.com`.
- Auth: WIF/keyless CI. Local service-account keys may exist for debugging but are not a frontend upload path.

## Invariants

- Do not manually upload widget assets with `gsutil`, `gcloud storage`, or ad-hoc shell deploys.
- If CI fails, inspect logs, fix the root cause, push, and re-run CI.
- Smoke-test failure is a real failure until proven otherwise.
- Keep environment mapping explicit: development/dev vs production/prod.

## Workflow

1. Identify the workflow and failing run:

   ```bash
   gh run list --repo PubKnow-Civic/askCivic --limit 10
   gh run view <run-id> --repo PubKnow-Civic/askCivic --log-failed
   ```

2. Fix the underlying code/config/IAM issue in a worktree.
3. Push and let CI run, or trigger the documented workflow dispatch only after verifying the workflow inputs in the repo:

   ```bash
   gh workflow list --repo PubKnow-Civic/askCivic
   gh workflow view <workflow> --repo PubKnow-Civic/askCivic
   ```

4. Verify the deployed surface:

   ```bash
   curl -sf https://dev.askcivic.com/ >/dev/null
   # Backend health URL depends on environment/service; use the repo's deploy config or workflow output.
   ```

## Common Failure Classes

- WIF/IAM failure: fix service account or bucket/Cloud Run permissions, then re-run CI.
- Build failure: fix package/code/env issue; do not upload artifacts manually.
- Widget smoke failure: verify Vite env injection and Clerk/API URL expectations.
- Cloud Run health failure: inspect service logs and config YAML/env before retrying.
