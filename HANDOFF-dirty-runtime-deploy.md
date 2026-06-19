# Handoff — A3 `dirty-runtime-deploy` (Live Runtime Reconcile + Enforcement Deploy)

Date: 2026-06-12 (Reboot Recovery Fleet). Status: **Phase 1 done; Deploy held — FG owns live sequencing.**

## Phase 1a — Live runtime reconcile ✅
- **Cause of dirtiness:** mode-only exec-bit flips `100644→100755` across ~50 `cli-tools/bin/*` (zero content change) — an SMB/worktree-sync artifact. `core.filemode=true` surfaced it → aborts `token-restart`.
- `.smbdeleteAAA18cf` is a **legitimately tracked** file (committed at 100644 in HEAD & origin/main) that caught the same flip — NOT deleted; mode-restored with the rest.
- **Reconcile:** `git -C …/live checkout -- cli-tools/bin` (modes stick — live is local APFS, not the SMB mount). Tree clean, ff-safe. Saved to memory: `live-runtime-exec-bit-flip-dirty`.

## Phase 1b — PR #195 ✅ MERGED
- Merge commit **`7ef1bb3`**, mergedAt **2026-06-13T01:35:10Z** (squash), now an ancestor of live HEAD.
- I rebased #195 onto #188 and resolved 11 `routes/hooks.py` conflicts (ported `_effective_parent` persona-suppression onto #188's v2 column migration: `existing_parent_id`/`old_parent_id`, not the dead `parent_instance_id` column).
- A **concurrent agent (resumed Malcador)** worked the same branch in the same worktree and committed `9e7869b` capturing the same three v2-interaction fixes I independently diagnosed (cross-validated): (1) bind persona_id from primarch for shared-legion personas (Malcador); (2) supplant self-retire — `singleton_guard_update` must exclude `OLD.id` on id-migration; (3) chapter-edge/`commander_type` sovereignty cascade. All 4 identity tests pass.
- Merge gate satisfied on main: #198, #199, #200, #195.

## Phase 2 — Deploy: HELD (do not run) ⏸
- **Deploy-1 already satisfied by other workers' restarts** (not my lane). FG: #201 restarted token-api to `c24e00e` @19:03. Runtime has since advanced to **#202 (`2751672`)**, port-7777 PID `10028`, tree clean, `/health` healthy. #195 enforcement code is live (ancestor of HEAD).
- I did **not** run `token-restart --sync`; confirmed before acting (live was already at the merge → `--sync` would no-op the ff and over-restart token-api+Discord+WSL, violating enforcement-only). **FG owns live deploy sequencing; A3 holds.**

## Open / for FG
- A4 verification of zombie-retirement (`6a8773e9` resolves, `d865db2e` retired) runs against the live token-api — FG dispatches.
- Coordination hazard observed: multiple agents editing `routes/hooks.py` / same worktree concurrently (me + Malcador, plus Task#5 lane). Converged safely this time.
- Stale PR worktree `wt-persona-pane-identity-hardening` left intact (merged with `--no-cleanup`); can be reaped.
