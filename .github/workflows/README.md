# Token-OS CI/CD

Token-OS is personal software on local boxes — there is no remote prod. "Deploy"
means **restart the right local services**. CI is a two-tier gate; CD is a
merge-triggered webhook fan-out that reaches each deploy host (the Mac + the k12
boxes) over Tailscale.

## Workflows

| Workflow | Trigger | Role |
|---|---|---|
| `push.yml` — *Push Gate (advisory)* | push to non-`main` branches | Tier 1. Job `push-advisory`. ruff format/lint, mypy, chill CodeRabbit — all **non-blocking** annotations. |
| `pr.yml` — *PR Gate (blocking)* | PR → `main` | Tier 2. Job **`quality`** (the required check). `ruff format --check` + `ruff check` + `mypy` **block**. |
| `secrets-scan.yml` | push/PR → `main` | Blocks on leaked IPs/secrets (patterns kept in repo secrets). |
| `deploy-prod.yml` — *Deploy (prod)* | push to `main` (merge) | CD fan-out: one matrix leg per host (mac, k12-personal, k12-work; `fail-fast: false`). Each leg: Tailscale ephemeral node → POST `/api/cd/restart` on that host (ack-first) → poll `/health` until `git_sha == github.sha`; mismatch after 180s is a deploy alarm/failure. A leg whose host IP secret is unset skips green (config-ready). |

### Ruff never-drift

The format-on-save hook (`cli-tools/scripts/post-tool-format.sh`) and the git
`pre-commit` hook both run **`uv run --python 3.11 --group dev ruff`** — the *exact*
lockfile-pinned ruff CI uses (no floating `uvx`). So local edits are byte-identical
to the gate and format drift never reaches CI. `worktree-setup` installs the
pre-commit hook and syncs both projects' dev venvs.

### Ops cockpit bundle refresh

Token-API serves the Vite build at `token-api/ui/ops` directly. PR CI does not
gate on committed bundle freshness. The local deploy path is authoritative:
`token-restart --sync` detects changes under `token-api/web/ops` or
`token-api/ui/ops`, runs `npm ci --no-audit --no-fund && npm run build` from
`token-api/web/ops` while the runtime checkout is unlocked, and aborts before the
Token-API restart if that required refresh fails. Generated runtime dirt confined
to `token-api/ui/ops` is discarded on the next deploy; mixed dirt is preserved via
the dirty-runtime WIP shunt.

## Branch protection (main)

Required status checks: **`quality`** (pr.yml) + **`secrets-scan`**. The advisory
push job is deliberately named `push-advisory` (not `quality`) so the required
check is unambiguous — it also fires on the PR head-branch push and would
otherwise collide by name.

## CD secrets (provisioned OUTSIDE the repo)

- `CD_RESTART_SECRET` — shared bearer; **must match** each host token-api's env of
  the same name. Mac: launchd plist (`~/Library/LaunchAgents/ai.openclaw.tokenapi.plist`,
  `EnvironmentVariables`; editing it needs `launchctl bootout`+`bootstrap`, not
  `kickstart`/`token-restart`). k12 boxes: the `token-api@live` systemd user
  unit's environment (e.g. the unit's `EnvironmentFile` `.env`), then
  `systemctl --user restart token-api@live`. Endpoint is fail-closed everywhere.
- `TAILSCALE_IP_MAC` — the Mac's tailnet IP (reused infra secret) = the Mac leg's webhook host.
- `TAILSCALE_IP_K12_PERSONAL` / `TAILSCALE_IP_K12_WORK` — the k12 boxes' tailnet
  IPs. While one is unset, that leg of the fan-out skips green (config-ready).
- `TS_AUTHKEY` — Tailscale auth key for the ephemeral CI node (tagged `tag:ci`); the
  tailnet ACL must allow `tag:ci` → `<host>:7777` for **each** provisioned host.

### Box-side deploy executor (k12)

On merge, the k12 legs' webhook spawns `cli-tools/bin/box-restart` (the Linux
analog of the Mac's `token-restart --sync`): ff-only bare-cache sync → detached
runtime checkout → frozen bun dep refresh when manifests changed → restart the
systemd user units whose files changed (`edge-proxy` / `k12-daemon` /
`tmuxctld`), always bouncing `token-api@live` last so `/health.git_sha` becomes
the deploy proof. It self-escapes into a transient `systemd-run --user --collect`
unit first, because a child in the token-api unit's cgroup would be killed by its
own `systemctl --user restart` (the Mac's setsid trick doesn't escape cgroups).

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
