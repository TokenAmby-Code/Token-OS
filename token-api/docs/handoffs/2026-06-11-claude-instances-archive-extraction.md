# Session Doc — `claude_instances` → `archive.db` extraction (Worker 1)

**Date**: 2026-06-11
**Branch**: `claude-instances-archive-extraction` (worktree `wt-claude-instances-archive-extraction`)
**Base**: `d096810` (== origin/main)
**Persona**: Mechanicus worker, dispatched by Fabricator-General
**Status**: 🟢 GREEN CORE COMMITTED (`ad7ef88`). 17/17 exterminatus tests pass. Schema extraction +
single-write mutation + full hooks.py repoint + the 4 legacy PATCH endpoints done. Remaining: a
mechanical read-site sweep (main.py ~85 sites, ~14 small modules, cli-tools, ~40 test files) — see the
RESUME PLAYBOOK directly below, written so a cheaper model can execute it without re-deriving anything.

---

## RESUME PLAYBOOK (cheap-model executable) — START HERE

**Goal of remaining work:** drive `git grep -l claude_instances` (non-archive, non-comment) to ZERO
across the repo, keeping every test GREEN, then run the SOP tail (restart → live-test → PR → merge).

**Ground rules (do not violate):**
- Work ONLY in this worktree: `/Users/tokenclaw/worktrees/Token-OS/wt-claude-instances-archive-extraction`.
  Never branch-switch or edit the live shared Token-OS checkout.
- NEVER `uv run`/`uv sync` against the live Token-API NAS `.venv` (it downs the service). Use this
  worktree's own `.venv` only. Test cmd is always `.venv/bin/python -m pytest ... -q` from `token-api/`.
- A formatter (ruff) runs after every Edit and WILL strip a newly-added import if the using code isn't
  present yet → `NameError`/`F821`. After adding an import, add the using code in the SAME edit, or guard
  the import with `# noqa: F401`. Re-grep the import after editing.
- RENAME/REPOINT ONLY — do not restructure control flow or response-shapes. A parallel branch
  (registration worker) rebases onto this file set; keep diffs surgical.

**The disposition map (legacy column → v2 home) is the single source of truth for every conversion.**
It is the "DISPOSITION MAP" / "Field disposition" section further down this doc. Summary:
- table `claude_instances` → `instances`.
- `tab_name` → `name` (alias `name AS tab_name` when a dict consumer needs the old key).
- status vocab: `processing`→`working`; `status IN ('processing','idle')` (="active") →
  `status NOT IN ('stopped','archived')`; `status != 'stopped'` → `NOT IN ('stopped','archived')`.
- `legion`/`primarch`/`profile_name` → `persona_id` (JOIN personas p ON p.id=i.persona_id, compare
  `p.slug`; emit `COALESCE(p.slug,'astartes') AS legion`; resolve a legion NAME to slug via
  `instance_registry.LEGACY_PERSONA_ALIASES`).
- `synced=1` ⇔ `golden_throne='sync'`. `instance_type`: 'sync'⇔marker='sync',
  'golden_throne'⇔marker not null and != 'sync' (a golden_throne.id), 'one_off'⇔marker IS NULL,
  'archived'⇔status='archived'.
- `parent_instance_id` → `commander_id` where `commander_type='chapter'` (alias
  `CASE WHEN commander_type='chapter' THEN commander_id END AS parent_instance_id`).
- `registered_at` → `created_at`. DEAD (remove): `session_id`, `pid`, `source_ip`, `tts_mode`
  (→notification_mode+interaction_mode). Annex columns keep their names (tmux_pane, pane_label,
  dispatch_*, workflow_*, planning_*, pr_*, victory_*, zealotry, gt_*, follow_up_sop, stop_allowed, …).
- Reference implementations already in-tree: `routes/hooks.py` (every pattern above) and the 4 PATCH
  endpoints in `main.py` (set_instance_legion/synced/type/archive). Helpers:
  `instance_mutation.create_golden_throne_binding`, `routes/hooks._launch_golden_throne_marker`,
  `routes/hooks._row_parent_instance_id`, `instance_registry.legacy_row_to_instance_values`.

**Order of execution (commit in coherent packages after each GREEN step):**
1. **main.py** (~85 sites). `git grep -n claude_instances main.py`. Convert per the map. The 9 plain
   `SELECT * ... WHERE id = ?` are pure table renames. Watch the `/api/instances` list (~11210),
   COUNT(*) active predicates, the dead `pid`/`working_dir` lookups (10474/10482), `WHERE legion=?`
   (20501), `SELECT legion` (10985). Verify: `git grep -c claude_instances main.py` == 0 (rewrite
   comments too), `.venv/bin/python -c "import ast; ast.parse(open('main.py').read())"`, then the
   exterminatus suite stays 17/17.
2. **Small token-api modules** (~14): tts.py, voice.py, day_start.py, db_helpers.py, shared.py,
   custodes_heartbeat.py, custodes_checkin.py, custodes_watchtower.py, morning_session.py,
   now_widget.py, cron_engine.py, talk.py, temp_message.py, session_doc_helpers.py,
   morning_supervisor.py, backfill_instance_id_stamps.py. (personas.py's
   `repair_legacy_instance_personas` reads the legacy table but is guarded by a table-exists check in
   init — it may stay, just confirm it no-ops post-drop.)
3. **cli-tools + @CC_STATE consumers** (separate processes; won't crash token-api but needed for
   completeness): dispatch, instance-stop, instances-clear, guardsman, notify, tmux-goto-spoken,
   tmux-multiprompt, tmux-pane-label, tmux-shuttle, open-session-doc, discord-routing, civic-thread,
   agents-db, send_gate.py, agent-session-end-resume.sh; vocab consumers tmux-base.conf:59,
   tmux-instance-exit, tmux-shuttle, lib/tmuxctl/assertions.py (accept 'working' etc.);
   discord-daemon/daemon.js; Scripts/engine-column-audit.py.
4. **Test fixtures** (~40 files seed/assert claude_instances): heaviest are test_legion_synced.py,
   test_instance_provenance.py, test_enforcement_core.py. Re-seed into `instances` v2 (use the
   `legacy_seed`-style fixture in test_claude_instances_exterminatus.py as the pattern).
5. **Full suite GREEN:** `.venv/bin/python -m pytest -q` (whole token-api/tests).
6. **SOP tail (task 8):** `token-restart` (NOT uv) → live-test: confirm `~/.claude/archive/archive.db`
   holds the extracted legacy rows (live pre-migration: claude_instances=144 rows / 3 processing,
   instances=167), `claude_instances` gone from the live DB, FG/custodes/admin identity reads correct
   from v2, ghost rows resolved, a fresh SessionStart round-trips → PR (pr-create) → CodeRabbit (NEVER
   `--admin-bypass`) → address → restart → live-test 2 → merge → restart. Fresh-clone completeness:
   `git ls-files` + import-graph check, and `git grep -l claude_instances -- ':!**/archive/**' ':!*.md'`
   returns nothing but intentional archive/restore code in db_schema.py.

**Already committed in `ad7ef88` (do NOT redo):** db_schema.py (archive extraction machinery +
triggers/indexes moved + legacy CREATE/migrations deleted), instance_mutation.py (single-write +
create_golden_throne_binding), instance_registry.py (IDENTITY+ANNEX columns + legacy_row mapping),
routes/hooks.py (FULL repoint), main.py (4 PATCH endpoints only), tests/test_claude_instances_exterminatus.py.

---

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

**Test state: 17/17 GREEN.** All of TestArchiveExtraction, TestSanctionedWritesV2Only,
TestFreshDatabase (incl. session_start_registers_into_instances_only + status_trigger_pushes_v2_vocab),
and TestLegacyPatchEndpoints (all 4) pass. Cmd:
`.venv/bin/python -m pytest tests/test_claude_instances_exterminatus.py -q`.

**COMMITTED:** `ad7ef88` (WIP checkpoint, branch `claude-instances-archive-extraction`, base d096810).
Contains the GREEN core: db_schema + instance_mutation + instance_registry + full routes/hooks.py
repoint + the four main.py PATCH endpoints. ruff clean.

**hooks.py: FULLY repointed** (the fleet-dispatched agent never landed; done manually). Every
claude_instances SQL site → instances; SessionStart/Stop/Prompt/PreTool/PostTool write paths all on
v2; dead cols (pid/session_id/source_ip) dropped; status writes use v2 vocab (working); legacy reads
derived — primarch→persona_id via LEGACY_PERSONA_ALIASES, parent_instance_id via
`_row_parent_instance_id` (commander_type='chapter'), instance_type via golden_throne marker,
synced→marker clear-if-'sync'. New helpers: `_launch_golden_throne_marker`,
`instance_mutation.create_golden_throne_binding`. NOTE: both new imports were stripped by the
formatter once — re-added; keep an eye out (the `# noqa: F401` on main.py's import guards it).

**main.py: 4 PATCH endpoints DONE** (/legion→persona_id+voice/sound; /synced→golden_throne='sync'
with persona-singleton 409; /type→marker semantics + GT-row mint via create_golden_throne_binding;
/archive+/unarchive→status+marker-clear). **~85 read-sites STILL PENDING** (see below).

**main.py remaining ~85 sites** (git grep -n claude_instances main.py): the `/api/instances` list
(~11210, `ORDER BY registered_at`→created_at, project legion via persona join, name AS tab_name,
status vocab, derive instance_type/synced from marker), cockpit/census COUNT(*) active predicates
(1865/1966/1973/18509/25246/25252: `status IN ('processing','idle')`→`NOT IN ('stopped','archived')`,
`='processing'`→`='working'`), pid/working_dir lookups (10474/10482 — pid column dead, drop or switch
to pane-stamp), `WHERE legion = ?` (20501→persona slug join), `SELECT legion` (10985→persona subquery),
tab_name/session_doc selects (2746/7876/9683/9760/10001/23890→name AS tab_name), plus the 9 plain
`SELECT * ... WHERE id = ?` (pure table rename). Test-only `DELETE FROM claude_instances` (1901)→instances.

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
  `claude-instances-archive-extraction`, base d096810 = origin/main. Checkpoint `ad7ef88` committed.
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
