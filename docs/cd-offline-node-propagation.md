# Cached CI/CD propagation for potentially-OFF nodes

**Status:** designed + WSL leg implemented (2026-07-14). Mobile leg design-only (follow-on).
**Contract:** device-doctrine ruling **#9** — WSL retires its deploy-node duty and becomes
the human contact surface; WSL and mobile are *potentially-OFF* nodes that must receive
deploys by **converging when they come online**, never by a merge-time push that silently
misses an off box.

## The problem

The merge-time CD pipeline (`.github/workflows/deploy-prod.yml`) is a **push**: on merge to
`main`, a GH-hosted runner joins the tailnet and POSTs each provisioned host's
`/api/cd/restart` webhook, which runs the box-local git-aware sync (`token-restart --sync`
on the Mac, `box-restart --sync` on the k12 boxes) and verifies `/health.git_sha` reached
the merged SHA.

A push only works for a box that is **on and reachable at merge time**. The k12 boxes are
always-on infra, so they stay in the fan-out. WSL and mobile are not:

- **WSL** = desk/games/Zoom surface. Off whenever the desk PC is off. Historically the
  Mac deploy leg pushed the WSL satellite runtime (`POST {wsl}/runtime/refresh`) on every
  merge — which silently no-ops (or fails) when WSL is off, leaving WSL stale until the
  next merge that happens to land while it is on.
- **Mobile** (Termux/MacroDroid) = off/asleep most of the time.

Ruling #9 retires this push duty. Deploys must instead **converge on the off node's own
boot/reconnect**, verified per node against the deploy record. No poll loops, no timeouts
guessing when the box wakes — the box tells us it woke by *booting*, and converges then.

## Deploy record — where deploy state is cached

Two tiers, so convergence never depends on a single box being up:

1. **GitHub `main` tip — the canonical deploy record.** GitHub is the sole code source of
   truth (ruling #10 / dev-echo #1). The merged SHA on `main` *is* the deploy target. Every
   node already has a git path to it (the box-local CD bare cache fetches `origin main`), so
   this tier is always reachable and needs no central service to be up.

2. **Hub token-api deploy record — the fast-path oracle + observability.** The always-on
   hub (the Mac today; **k12-personal** at cutover, per ruling #2/#3) records the last
   deploy it was told to ship. `POST /api/cd/restart` now persists a durable record
   `{sha, pr_url, deployed_at}` to `~/.claude/cd-deploy-record.json` (runtime-writable,
   survives a hub restart), exposed read-only and tokenless at:

   ```
   GET /api/cd/current  →  {"sha": "...", "pr_url": "...", "deployed_at": "...", "source": "record"|"launched"}
   ```

   If no record exists yet (fresh hub), it falls back to the hub's own `LAUNCHED_GIT_SHA`
   (`source: "launched"`) so the endpoint is always truthful about what the fleet is
   serving. This tier carries `pr_url`/timestamp metadata and lets an off node answer "am I
   stale?" with one cheap HTTP GET instead of a git fetch.

A converging node prefers the hub oracle (tier 2) and **falls back to GitHub (tier 1)** when
the hub is unreachable — so a node still converges even if the hub is *also* off (e.g. both
came up together). The hub record and `main` tip are eventually identical; the hub may lag
`main` by at most the coalesce window of one in-flight deploy, which the GitHub fallback
covers.

## How an off node learns it is stale — boot/reconnect, event-driven

The convergence logic lives **behind an endpoint** on the node (endpoint-first; CLI/systemd
are thin callers). On WSL that is the satellite's:

```
POST /cd/converge  →  {node, was, target, source, now, converged, action}
```

`/cd/converge` performs one idempotent check — **no loop, no timeout, no debounce**:

1. **Resolve the target SHA.** GET `{hub}/api/cd/current` → `.sha`; on any failure fall
   back to `git ls-remote {git_url} refs/heads/main`. The hub URL comes from
   `$TOKEN_API_URL` → `imperium_config.cfg("token_api_url")` (migrates Mac→k12-personal via
   the registry; never hardcoded). Records which `source` answered.
2. **Compare** the target to the node's own live `/health` git_sha (`_runtime_git_sha()`).
3. **If equal** → `already_current`, return. This is the fixpoint.
4. **If stale** → compute the changed-path manifest the way a pusher would
   (`git diff --name-only <current>..<target>` out of the freshly-fetched bare cache) and
   run the box-local git-aware deploy — the existing `token-satellite-refresh` helper,
   invoked through the same `_spawn_runtime_refresh()` path the authenticated
   `/runtime/refresh` push used. The helper detach-checks-out the target, refreshes venv/AHK
   only for the paths that actually changed, and restarts the satellite.

**Triggers (the "boot/reconnect event"):**

- **Boot** — the satellite fires `_cd_converge()` once in a background thread from its
  FastAPI `startup` event (beside the existing `_announce_to_mac`). Every satellite start
  (cold boot, systemd `Restart=always`, or the deploy-driven self-restart) re-checks against
  the deploy record. A stale runtime self-heals; a current one no-ops.
- **Reconnect** — the same endpoint is what a tailnet-up edge or a manual operator poke
  drives. Wiring the tailnet-up edge to re-fire `/cd/converge` for the *box-stayed-up,
  tailnet-dropped-during-a-merge* case is a small follow-on (the endpoint already exists;
  only the trigger is pending — see Follow-ons).

Because a stale-triggered convergence *restarts the satellite*, verification lands on the
**next boot's** convergence check, which finds `already_current`. That is the natural
convergent loop — it terminates in exactly one restart and cannot thrash (the helper holds a
flock, and a current runtime never spawns a refresh).

## How convergence is verified per node

Same proof as the merge-time pipeline: **`/health.git_sha` parity against the deploy
record.** After a node converges, its `/health.git_sha` equals the target SHA. Verification
is done by the observer, not by a self-poll:

- The convergence response reports `was`/`target`/`now`/`converged`.
- Any observer (the hub, a CLI, this session's harness) confirms by polling the node's
  `/health` until `git_sha == target` — exactly `verify_wsl_git_sha` /
  `Verify live deployment SHA` already do for the push path.

## What changed (WSL leg)

| Surface | Change |
|---|---|
| `token-api/main.py` | Persist `{sha, pr_url, deployed_at}` in `cd_restart`; add tokenless `GET /api/cd/current` (deploy-record oracle, `LAUNCHED_GIT_SHA` fallback). |
| `token-api/token-satellite.py` | Add `_cd_converge()` + `POST /cd/converge`; fire it once on the satellite `startup` event; resolve the hub URL from config; factor `_spawn_runtime_refresh()` shared by `/runtime/refresh` and convergence. |
| `cli-tools/bin/token-restart` | **Retire** the merge-driven WSL push: the Mac deploy leg no longer sets `REFRESH_WSL`/`RESTART_WSL`. WSL is no longer a push target — it converges on its own boot. Operator-driven `--wsl-only` / manual restart are unchanged. |

The k12 boxes and the Mac are untouched as always-on push targets. The WSL leg of the Mac
deploy is excised, not shimmed (no compat fallback that could silently re-push).

## Mobile (Termux/MacroDroid) — design-only follow-on

Mobile is off/asleep even more than WSL and has no git checkout to converge — the phone runs
MacroDroid macros + a Termux HTTP surface, and receives deploys as artifact/config pushes
(webhook base-url + macro imports), not code checkouts. The propagation shape mirrors WSL
but the "deploy" is different:

- **Deploy record for mobile** = the same hub `GET /api/cd/current` (SHA + timestamp), plus
  the mobile-relevant artifact set (the locked `.macro` import bundle + the MacroDroid
  base-url global). The phone caches the last-applied SHA locally.
- **Staleness on reconnect** = a MacroDroid boot/connectivity trigger fires a Termux caller
  that GETs `{hub}/api/cd/current`, compares to the phone's last-applied SHA, and if stale
  pulls the current macro bundle via the locked import path (`/mobile` skill) and re-applies
  the base-url global.
- **Verification** = the phone POSTs its applied SHA back to a mobile deploy-state endpoint
  (telemetry POST — the `mobile+WSL` pairing in ruling #9), so the hub can show mobile
  convergence the way `/health.git_sha` shows it for boxes.

Scoped as a follow-on because it needs the MacroDroid boot-trigger macro + the locked import
path wiring on the phone (see the `mobile` skill), which is device-ceremony work, not a
repo-only change. No mobile code ships in the WSL PR.

## Follow-ons

1. **Mobile leg** — above.
2. **WSL tailnet-reconnect trigger** — fire `/cd/converge` on a tailnet up-edge (not just
   boot) for the box-stayed-up, tailnet-partitioned-during-a-merge case. Endpoint already
   exists; only the edge hook is pending.
3. **Cutover** — when the Token-API home moves Mac→k12-personal, the hub URL follows
   automatically via `imperium_config` (`token_api_url` registry row); no code change.
