# Session Doc — `claude_instances` → `archive.db` extraction (Worker 1)

**Date**: 2026-06-11
**Branch**: `claude-instances-archive-extraction` (worktree `wt-claude-instances-archive-extraction`)
**Base**: `d096810` (== origin/main)
**Persona**: Mechanicus worker, dispatched by Fabricator-General
**Status**: 🟡 BLOCKED on scope confirmation — premise contradicted by ground truth. No code written yet (investigation only).

## Mandate (as briefed)

Totally extract legacy `claude_instances` into a separate `archive.db`; make v2 `instances`
the sole authoritative identity source (no longer *projected* from `claude_instances`);
remove/repoint legacy PATCH endpoints (`/legion`, `/synced`, `/type`); idempotent + reversible
migration; verify row counts.

## Ground-truth findings (read-only investigation)

The brief's premise — that `claude_instances` is a vestigial legacy table only used to *project*
identity into v2 — does **not** match the live system:

1. **`claude_instances` is the LIVE operational table.** 153 `FROM claude_instances` read-sites in
   non-test source vs only 14 `FROM instances`. main.py (90), routes/hooks.py (30), tts/voice,
   now_widget, cron_engine, talk, temp_message, morning_supervisor, db_helpers, shared all read it
   for runtime state (status, tmux_pane, synced, instance_type, session_doc_id, ...).

2. **The runtime DUAL-WRITES to both tables.** `instance_mutation.py`
   (`sanctioned_insert_instance` @202/227, `sanctioned_update_instance` @286) mirrors every mutation
   into both `claude_instances` (legacy vocab: `processing`/`idle`/`stopped`) and `instances` v2
   (new vocab: `working`/`idle`/`victorious`/...). Confirmed live: identical
   `max(last_activity)` = `2026-06-11T09:09:36.994772` and identical `registered_at` in both tables;
   the 3 active sessions are `processing` in legacy ↔ `working` in v2 (same row ids).

3. **v2 has diverged / accumulated ghost rows.** Live counts: `claude_instances`=144,
   `instances`=167. Several v2 rows are `working`/`idle` but `stopped`/absent in `claude_instances`
   (e.g. `d865db2e` working in v2, absent from legacy). These are the "ghost rows" the brief noted.

4. **The projection is migration-time only.** `_ensure_instances_v2()` (db_schema.py:138-161) rebuilds
   v2 from `claude_instances` ONLY when the v2 schema changes (`needs_rebuild`). On steady-state boot
   it keeps existing v2 rows. The default-leakage (rank=`astartes`, commander=`emperor`,
   origin=`local`) the brief flagged comes from `legacy_row_to_instance_values()` at that rebuild.

5. **A coordinated, slice-based cutover is ALREADY in flight across ≥4 other branches** — the brief's
   coordination model only mentioned ONE parallel worker (session-registration):
   - `custodes-sync-decouple-rank` — decoupling the exact `synced=1 AND instance_type='sync'` custodes
     markers the brief told me to repoint (resolve by persona+rank instead). **Overlaps my D3.**
   - `cutover-slice-a-pane-state-worker` — pane-state worker reads off legacy.
   - `cutover-slice-b-reverse-lookups` — reverse pane/session-doc lookups → @INSTANCE_ID stamps.
   - `session-registration-deferred-artifacts` — the briefed parallel worker (same base as me).

## The contradiction

Delivering D1+D2 literally ("drop `claude_instances` from the live DB; no read path uses it") =
**completing the entire cutover** = rewriting 153 read-sites + removing the dual-write, which:
- breaks the live runtime if done before the read-sites are cut over, and
- collides head-on with the 4 in-flight cutover branches (esp. custodes repoint duplication).

This is not a wholesale single-branch change that "lands first" — it is the *culminating* step of a
cutover other workers are still slicing.

## Proposed phasing (pending FG/Emperor go-ahead)

**Phase 1 — safe, additive, lands now (no live drop, no read-site rewrites):**
- Build `archive/` folder + `archive.db`; idempotent + reversible migration that COPIES
  `claude_instances` (schema + rows) into `archive.db`, verifying row counts in==out (D1 copy + D4).
- Stop the *ongoing projection*: `_ensure_instances_v2` rebuilds v2 from existing v2 rows only,
  never re-deriving from `claude_instances` (fresh-DB bootstrap preserved; addresses ghosts + FG row).
- One-shot backfill fixing existing v2 identity fields (rank/commander_type/origin_type) the original
  projection left at defaults (FG/custodes/admin read correctly).
- Repoint/remove legacy PATCH `/legion`, `/synced`, `/type` → v2 columns (coordinate w/ custodes branch).

**Phase 2 — gated on the cutover slices merging (FG sequencing decision):**
- Actually DROP `claude_instances` from the live DB and remove the dual-write.
- Final cutover of remaining read-sites.

## Scope ruling (Emperor, 2026-06-11)

**Exterminatus declared on `claude_instances`. Full extraction in this branch.** Direct quote of
intent: "instances is THE new central table … we must eat the pain up front to avoid a
stagnation/burnout dual-db superdisaster … every moment putting makeup on that corpse warps
architecture and confuses agents." Staged commits are fine; a surviving live legacy table is not.

## Field disposition map (legacy `claude_instances` column → home)

**Already in v2 (rename / vocab map):**
`id`→`id` · `tab_name`→`name` (API responses alias `name AS tab_name` for shape stability) ·
`engine` · `working_dir` · `device_id` · `origin_type` · `status` (vocab: `processing`→`working`;
active-set predicates become `status NOT IN ('stopped','archived')`) · `registered_at`→`created_at`
· `last_activity` · `stopped_at` · `session_doc_id` · `continuity_binding_source` ·
`wrapper_launch_id`

**Derived in v2 (reads repoint to the derivation, column dies):**
- `legion`/`primarch`/`profile_name` → `persona_id` (JOIN `personas` ON slug; custodes resolution
  `legion='custodes' AND synced=1` → persona slug `custodes` + `golden_throne='sync'`)
- `instance_type='sync'`/`synced=1` → `golden_throne='sync'`
- `instance_type='golden_throne'` → `golden_throne IS NOT NULL AND golden_throne != 'sync'`
- `parent_instance_id` → `commander_type='chapter'`, `commander_id`
- `tts_mode` → `notification_mode` + `interaction_mode`

**Runtime annex (NEW columns on `instances`, explicitly transitional, each slated for removal by
the in-flight stamp/golden-throne slices):**
`tmux_pane`, `pane_label`, `dispatch_target`, `dispatch_window`, `dispatch_mode`, `dispatch_slot`,
`dispatch_session_doc_path`, `target_working_dir`, `launch_mode`, `launcher`,
`transplant_target_session`, `transplant_expected`, `input_lock`, `tts_voice`,
`notification_sound`, `discord_hosted`, `discord_channel`, `discord_bot`, `workflow_state`,
`workflow_updated_at`, `workflow_blocked_reason`, `next_required_action`, `next_action_owner`,
`planning_state`, `planning_updated_at`, `planning_source`, `closure_surface`, `closure_required`,
`session_doc_policy`, `pr_url`, `pr_state`, `victory_at`, `victory_reason`, `is_subagent`,
`hook_driven`, `zealotry`, `gt_resume_count`, `gt_resume_window_started_at`, `gt_last_resume_at`,
`follow_up_sop`, `stop_allowed`.
Rationale: faithful preservation — exterminatus kills the TABLE, it does not redesign flag
semantics mid-flight (`is_subagent` vs `hook_driven` vs `automated` stay distinct; GT fields stay
until the `golden_throne`-table cutover lands).

**Archive-only (dead, preserved in archive.db only):**
`session_id`, `source_ip`, `pid` (verify zero live reads during implementation).

## PROGRESS (updated pre-compact, 2026-06-11)

**Done (tasks 1–4):**
1. ✅ Disposition map (below) — settled, instance_type/synced columns DIE (golden_throne marker is
   sole truth); GT data fields (zealotry/gt_*/follow_up_sop/stop_allowed) stay annex.
2. ✅ RED tests: `tests/test_claude_instances_exterminatus.py` (17 tests; was 13 RED / 4 invariant-green).
3. ✅ `db_schema.py`: annex columns in `_create_instances_table`; `_ensure_instances_v2` no longer
   reads claude_instances (rebuilds from v2 rows only); `archive_db_path_for()` (env
   `TOKEN_API_ARCHIVE_DB`, default `<db dir>/archive/archive.db`); `_extract_claude_instances()`
   (copy→verify counts→backfill annex/persona/golden_throne marker→FK-free rebuild of
   instance_mutations+workflow_events→DROP); `restore_claude_instances_from_archive()` (reverse
   path); legacy CREATE/migrations/indexes deleted; 7 triggers moved to `instances`
   (trg_status_pane_state, trg_planning_pane_state, trg_tab_name_pane_state [tab_name→name],
   trg_doc_sync_* ×4) — DROP+CREATE pattern; new idx_instances_v2_gt + idx_instances_v2_discord.
4. ✅ `instance_registry.py`: IDENTITY_COLUMNS + RUNTIME_ANNEX_COLUMNS (41 cols) = INSTANCE_COLUMNS;
   legacy_row_to_instance_values respects explicit v2 fields + annex passthrough;
   REMOVED_INSTANCE_COLUMNS now only truly-dead cols with inline derivation notes.
5. ✅ `instance_mutation.py`: single-write to `instances` everywhere; mirror layer deleted;
   `sanctioned_update_legacy_runtime_fields` → `sanctioned_update_runtime_fields` (+_sync twin);
   `sanctioned_insert_instance` canonicalizes legacy- or v2-shaped dicts (+_sync twin new);
   INSTANCE_MUTATION_FIELDS rewritten to v2+annex names; chapter-commander legacy fallback removed.
6. ✅ `routes/hooks.py` (partial): import + `_persist_legacy_runtime_fields`→`_persist_runtime_fields`
   (4 call sites renamed) — REST OF hooks.py STILL PENDING (see below).

**Test state:** 11/17 GREEN. All TestArchiveExtraction (6) + TestSanctionedWritesV2Only (2) +
fresh-DB schema tests pass. Remaining 6 RED need main.py + hooks.py repoints:
session_start_registers_into_instances_only, status_trigger_pushes_v2_vocab, 4× PATCH endpoint tests.

**In flight:**
- **main.py repoint dispatched to fleet pane %116** (session doc
  `Mars/Sessions/2026-06-11-catch-repoint-main-py-to-instances-v2.md`) — full disposition map +
  PATCH endpoint semantics in its brief. CHECK ITS RESULT before continuing main.py work.
- **hooks.py repoint agent FAILED to start (API 529, 0 tokens, agent id ab106227bc467750a)** — needs
  re-dispatch or do manually. Full prompt spec was: repoint ~30 SQL sites + ~120 python-side legacy
  field reads (tab_name×12, legion×14, instance_type×19, synced×7, profile_name×5,
  parent_instance_id×20, registered_at×2, pid×10, session_id×10, primarch×14, processing×12);
  RENAME ONLY, no flow restructuring (registration worker rebases onto this file). Key sites:
  660, 685, 907, 924, 946 (drop `OR session_id = ?`), 971, 1130, 1293, 1947, 1956, 1979, 2007
  (primarch supplant → p.slug), 2027 (PID+pane match: drop `AND pid = ?`, keep pane-stamp gate),
  2219, 2387, 2645, 2706 (parent legion → persona slug subquery), 3030, 3140, 3215, 3235+3365+4288
  ({"status":"processing"}→"working"), 3439 (instance_type→golden_throne=='sync'), 3688, 4156,
  4311, 4364, 4462, 4532, 4809, 4864; SessionStart insert dict ~2715 (drop session_id/source_ip/pid
  keys; legacy-shaped rest auto-canonicalizes); PERSONA_PANE_IDENTITY writes → persona_id +
  golden_throne='sync'.

**Still pending (tasks 5–8):**
- 14 small token-api files (mine): tts.py, voice.py, day_start.py, db_helpers.py, shared.py,
  personas.py (repair_legacy_instance_personas reads legacy — guarded by table-exists in init, can
  stay), custodes_heartbeat.py, custodes_checkin.py, custodes_watchtower.py, morning_session.py
  (API-shape consumers: accept v2 status vocab), now_widget.py, cron_engine.py, talk.py,
  temp_message.py, session_doc_helpers.py, morning_supervisor.py, backfill_instance_id_stamps.py.
- cli-tools direct-DB readers (dispatch, instance-stop, instances-clear, guardsman, notify,
  tmux-goto-spoken, tmux-multiprompt, tmux-pane-label, tmux-shuttle, open-session-doc,
  discord-routing, civic-thread, agents-db, send_gate.py comment, agent-session-end-resume.sh) +
  @CC_STATE vocab consumers (tmux-base.conf:59 comment, tmux-instance-exit, tmux-shuttle,
  lib/tmuxctl/assertions.py — must accept v2 vocab 'working' etc.) + discord-daemon/daemon.js +
  Scripts/engine-column-audit.py.
- ~40 test files seed/assert claude_instances → v2 fixtures (task 7).
- Full SOP tail (task 8): GREEN → token-restart → live-test (verify archive.db at
  ~/.claude/archive/archive.db holds 144 legacy rows, claude_instances gone from live DB,
  FG/custodes identity reads correct, ghost rows resolved) → PR → CodeRabbit → live-test 2 → merge.
  Live DB pre-migration state: claude_instances=144 rows (3 processing), instances=167.

**Key facts for resume:**
- Worktree: `/Users/tokenclaw/worktrees/Token-OS/wt-claude-instances-archive-extraction`, branch
  `claude-instances-archive-extraction`, base d096810 = origin/main. NOTHING COMMITTED YET.
- Test cmd: `.venv/bin/python -m pytest tests/test_claude_instances_exterminatus.py -q` (from token-api/).
- Formatter hook auto-runs on edits (once stripped my new imports mid-edit — re-add if NameError).
- Emperor's ruling verbatim is in "Scope ruling" above; full extraction in THIS branch, staged
  commits OK, no compat shims/makeup.
- Coordination: registration worker (wt-session-registration-deferred-artifacts, same base) rebases
  onto my result. custodes-sync-decouple-rank overlaps the custodes predicate repoints — my v2 form
  (p.slug='custodes' AND golden_throne='sync') is the forward-compatible one.

## Migration design

`~/.claude/archive/archive.db` (override `TOKEN_API_ARCHIVE_DB`); one-shot, idempotent, reversible:
1. Copy `claude_instances` schema + ALL rows → archive.db; verify `count(in) == count(out)`.
2. Rebuild `instances` with annex columns: existing v2 rows are authoritative; matching legacy rows
   fill annex fields only. (Inverts the old `or source_rows` priority at db_schema.py:145 — the
   ghost-row / FG-row bug.) Legacy-only rows do NOT enter live `instances`.
3. Rebuild `instance_mutations` + `workflow_events` WITHOUT the `REFERENCES claude_instances(id)`
   FK (else `PRAGMA foreign_keys=ON` poisons every insert after the drop).
4. Recreate `pane_state_queue` triggers on `instances` (pushes v2 status vocab → update @CC_STATE
   consumers: tmux-base.conf, tmux-instance-exit, tmux-shuttle, tmuxctl/assertions.py).
5. `DROP TABLE claude_instances` + its 5 indexes. Fresh DBs never create it.
Reverse path: documented restore (copy table back from archive.db) — archive.db is a full snapshot.
