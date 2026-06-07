# Task: (A) Decouple the morning keepalive from `instance_type=='sync'`, and (B) surgically remove the Discord sync-session lock

You are a legion (astartes) worker. Stay in your isolated worktree; never touch `main` directly. Deliver a PR.

Two related fixes in one PR. Both stem from the same root error: **`instance_type=='sync'` / `synced=1` is being (ab)used as a routing/identity signal, when the authoritative identity is now the singleton persona pane (`legion:custodes` marker).** The sync-session lookups are an old routing hack from BEFORE persona panes + the singleton lock existed. Discord must not have any perspective on sync sessions — that's a token-api-internal concern.

---
# PART A — Morning keepalive decouple

## The bug (root cause of the "morning session still active" sisyphus loop)
`token-api/routes/hooks.py` ~line 3175, in the Stop hook:
```python
if instance_type == "sync":
    # accept the Stop, then re-inject MORNING_KEEPALIVE_PROMPT (timestamped) via claude-cmd --pane
```
This is the ENTIRE gate. There is **no** check on:
- whether a morning session exists for today,
- the morning session's `status` (launched vs ended),
- time-of-day / any upper time bound.

The code comment even admits: *"The only exit is flipping the instance off `sync`."* So it is not a *morning* keepalive — it is a **sync keepalive**: ANY `instance_type=='sync'` instance re-injects "the morning session is still active…" on every Stop, indefinitely.

**Why this is critical:** the Custodes singleton REQUIRES `instance_type=='sync'` (the state-hook dispatcher `_dispatch_custodes_intervention` needs synced=1 AND type=sync; `SessionStart` stamps Custodes `sync`). So a correctly-registered Custodes **always** triggers this loop and runs into the afternoon/evening. Witnessed 2026-06-02: fired at 16:46 and again 17:56 long after morning, ignoring `morning status=ended`. The only workaround was demoting to `type=one_off` — which sacrifices state-hook interventions.

## Fix
Gate the keepalive on an **active morning session**, not on `type=='sync'`:
1. Re-inject ONLY when ALL hold:
   - a morning session record exists for today (`/tmp/custodes_morning_sessions/morning_<date>.json`), AND
   - its `status == "launched"` (NOT `"ended"`), AND
   - it is within an upper **time bound** measured from `started_at` — the Emperor's **2-hour auto-disable** (make the bound a named constant, e.g. `MORNING_MAX_DURATION_HOURS = 2`). Past the bound: do NOT re-inject; instead auto-end the session (see #3).
2. Keep `instance_type=='sync'` as a NECESSARY but not SUFFICIENT condition (a non-sync instance should never get the keepalive), so the Custodes singleton can stay `sync` for state-hooks WITHOUT looping once the morning session is ended/expired.
3. Fix `POST /api/morning/end` (and the new auto-expiry path) to **also write the state-file `status="ended"`** — today it only flips `instance_type` to `one_off`, leaving `status="launched"`, so the status check would still pass. The end/auto-disable must durably flip the file status.
4. When the 2-hour bound trips, auto-end the session (write `status="ended"`, set `ended_by="auto-2h-bound"`) and emit one final notice — do NOT keep re-prompting.

## Verify (Part A)
- Regression test: a `sync` instance with NO active morning session (or `status="ended"`, or past the 2h bound) gets a clean Stop with **no** keepalive re-injection.
- Regression test: a `sync` instance WITH an active, in-bound morning session DOES get the keepalive (preserve the intended behavior during a real morning).
- Regression test: `morning/end` writes `status="ended"` to the state file (not just the instance_type flip).
- Run the token-api hooks tests + morning_session tests.

---
# PART B — Remove the Discord sync-session lock (resolve via the `legion:custodes` singleton pane)

## The principle (Emperor, 2026-06-02)
Discord does **not** need any perspective on sync sessions — that's a token-api-internal concern. Resolving the Discord target by hunting for a live/`synced` DB row is an old routing hack from before persona panes + the singleton lock. The authoritative Custodes identity is the **`legion:custodes` tmux pane marker** (the singleton lock). Discord routing must resolve Custodes deterministically via that marker, NOT via `instance_type=='sync'` / `synced=1` / "find any live row."

## What to change (anchors — audit the whole chain, these are the known ones)
1. **`token-api/main.py` `_try_discord_injection()` (~18724-18788):** for `legion=='custodes'` the primary resolution must be the singleton pane (`_find_custodes_tmux_pane()` / `_assert_and_send_custodes()`), NOT the leading `SELECT ... WHERE legion='custodes' AND status IN ('idle','processing') LIMIT 1` DB-row hunt. Collapse Custodes onto the deterministic singleton-pane path; the DB-row lookup should at most supply the `instance_id` for an already-identified pane, never be the thing that decides the target. Remove the `require_synced` perspective for the Custodes path entirely.
2. **`cli-tools/bin/discord-routing` (~109-167):** the diagnostic models Custodes routing off live/`synced` DB rows and emits `synced`-centric issues/fixes. Re-model the Custodes verdict around the `legion:custodes` pane marker (pane exists + alive ⇒ OK), not around `synced`/live-row presence. Keep the tool honest: its verdict must match the new injection logic.
3. **Audit** `routes/voice.py`, `routes/tts.py`, and any Discord voice path for residual `synced`/`instance_type=='sync'` gating of the Custodes target; convert to singleton-pane resolution.

## Scope / DO-NOT-BREAK
- This is about the **Custodes singleton** path. **Mechanicus's `require_synced=True` pattern is intentional and separate** — do NOT rip it out as part of this. If you find Mechanicus is *also* a singleton persona pane (`mechanicus:fabricator-general`) that should follow the same marker-based resolution, NOTE it in the PR body as a follow-up; do not bundle that change here unless it's the identical lock.
- Surgical. Don't refactor unrelated Discord plumbing.

## Verify (Part B)
- Regression test: Custodes Discord injection resolves the target via the `legion:custodes` pane marker with **no** `synced`/`sync`-session query in the path; succeeds even when the DB row is stale/`one_off`/`synced=0` as long as the pane is alive.
- Regression test (or manual note): `discord-routing` reports Custodes `Verdict OK` based on the pane marker, independent of `synced`.
- Confirm `tests/test_legion_synced.py` still passes for the Mechanicus path; update its Custodes expectations to the marker-based model.

## Deliverable
ONE PR onto clean `main` covering BOTH parts. Title: `fix(custodes): decouple morning keepalive + Discord routing from instance_type=='sync' (resolve via legion:custodes singleton)`. Body: (A) before/after keepalive gate + 2h auto-disable + morning/end status-write; (B) before/after Discord Custodes resolution (DB-sync-hunt → singleton-pane marker), what `require_synced` perspective was removed, the Mechanicus-scope note. Confirm all tests. Report the PR number.

## Context for the dispatcher (Custodes)
Witnessed 2026-06-02. Interim mitigation in prod RIGHT NOW: the live Custodes row is `type=one_off, synced=1` (loop dead, Discord still routes — Discord needs only `synced=1`+alive, not sync). This is a stopgap; every future sync Custodes loops until this lands. Pairs with the staged morning-hook delivery-proof fix (`.dispatch-prompts/morning-hook-direct-pane-write-20260602.md`) — same files, coordinate the merge. `hooks.py` has uncommitted WIP on main; branch from committed main.
