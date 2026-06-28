# Token-OS CI/CD

Token-OS is personal software on a local Mac — there is no remote prod. "Deploy"
means **restart the right local services**. CI is a two-tier gate; CD is a
merge-triggered webhook that reaches the Mac over Tailscale.

## Workflows

| Workflow | Trigger | Role |
|---|---|---|
| `push.yml` — *Push Gate (advisory)* | push to non-`main` branches | Tier 1. Job `push-advisory`. ruff format/lint, mypy, pytest, chill CodeRabbit — all **non-blocking** annotations. pytest runs under `pytest-xdist -n auto --dist loadfile` (~7 min on a 10-core box vs ~20 min single-threaded). |
| `pr.yml` — *PR Gate (blocking)* | PR → `main` | Tier 2. Job **`quality`** (the required check). `ruff format --check` + `ruff check` + `mypy` **block**. **pytest is NOT yet on this hot path** — re-adding it as a blocking gate is the intended next step now that xdist makes it fast, but it is blocked on test-suite hermeticity (the suite still flakes under parallel execution). The full suite is preserved in `prod-gate.yml` and runs advisory in `push.yml`. |
| `prod-gate.yml` — *Prod Gate (tests)* | PR/push → `prod`, `workflow_dispatch` | Full pytest suite under `pytest-xdist -n auto --dist loadfile`, RESERVED for the future prod-branch CI. Inert until a `prod` branch exists — never runs on PRs into `main`, so it can't block the hot path. Run on demand via the Actions tab. |
| `secrets-scan.yml` | push/PR → `main` | Blocks on leaked IPs/secrets (patterns kept in repo secrets). |
| `deploy-prod.yml` — *Deploy (prod)* | push to `main` (merge) | CD. Tailscale ephemeral node → POST `/api/cd/restart` on the Mac (ack-first) → poll `/health` until `git_sha == github.sha`; mismatch after 180s is a deploy alarm/failure. |

### Ruff never-drift

The format-on-save hook (`cli-tools/scripts/post-tool-format.sh`) and the git
`pre-commit` hook both run **`uv run --python 3.11 --group dev ruff`** — the *exact*
lockfile-pinned ruff CI uses (no floating `uvx`). So local edits are byte-identical
to the gate and format drift never reaches CI. `worktree-setup` installs the
pre-commit hook and syncs both projects' dev venvs.

### Running tests locally

Both suites default to **parallel** locally via `addopts = "-n auto --dist loadfile"`
in each `pyproject.toml` `[tool.pytest.ini_options]` — a bare `pytest` (or an agent
session that shells out to it) gets the xdist speedup for free, no `-n` flag to remember.

- **token-api** runs parallel everywhere — local *and* CI (the CI commands already pass
  `-n auto --dist loadfile` explicitly; the addopts is a harmless duplicate).
- **cli-tools** runs parallel **locally** but is pinned **serial on CI** via an explicit
  `pytest -n0 …` in all three workflows (the contended GH runner is unverified for
  cli-tools parallel, so CI keeps the proven serial behavior).
- To **debug**, disable parallelism with `pytest -n0` (required for `pdb`, `-s`, and
  reliable single-test runs). Removing the `addopts` line reverts that suite to
  serial-default — a local-only knob, independent of branch protection.

## Branch protection (main)

Required status checks: **`quality`** (pr.yml) + **`secrets-scan`**. The advisory
push job is deliberately named `push-advisory` (not `quality`) so the required
check is unambiguous — it also fires on the PR head-branch push and would
otherwise collide by name.

## CD secrets (provisioned OUTSIDE the repo)

- `CD_RESTART_SECRET` — shared bearer; **must match** token-api's launchd-plist env
  of the same name (`~/Library/LaunchAgents/ai.openclaw.tokenapi.plist`,
  `EnvironmentVariables`). Endpoint is fail-closed. NOTE: editing the plist env
  needs `launchctl bootout`+`bootstrap`, not `kickstart`/`token-restart`.
- `TAILSCALE_IP_MAC` — the Mac's tailnet IP (reused infra secret) = the webhook host.
- `TS_AUTHKEY` — Tailscale auth key for the ephemeral CI node (tagged `tag:ci`); the
  tailnet ACL must allow `tag:ci` → `<Mac>:7777`.

### TODO — migrate `TS_AUTHKEY` → Tailscale OAuth client (by Sep 2026)

`tailscale/github-action` now recommends an **OAuth client** over a raw auth key,
and the `tailscale/github-action` run warns about it. The current raw `TS_AUTHKEY`
**expires September 2026** — use that forced rotation as the moment to switch:

1. Tailscale admin → **Settings → OAuth clients → Generate** with scope
   `devices:write` (or `auth_keys`) and tag `tag:ci`.
2. Add repo secrets `TS_OAUTH_CLIENT_ID` + `TS_OAUTH_SECRET`.
3. In `deploy-prod.yml`, replace the `authkey:` input with
   `oauth-client-id:` / `oauth-secret:` + `tags: tag:ci`.
4. Delete the `TS_AUTHKEY` secret. (OAuth clients don't expire like auth keys, so
   this also ends the annual rotation chore.)
