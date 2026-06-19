# Handoff — GT reader-filter (retired-seat phantom-dispatch fix)

**Status: READY, HELD FOR FG.** Do NOT self-merge — FG batches the persona-assert
+ reader-filter live deploys (merge to main auto-fires CD → `token-restart --sync`
→ live token-api restart). A4 verification is COMPLETE; #201 already restarted the
live runtime to c24e00e (19:03). Live runtime was NOT touched by this work.

## PR
- **#203** — https://github.com/TokenAmby-Code/Token-OS/pull/203
- Branch `gt-reader-filter`, HEAD **3ecbaa1**, rebased on `origin/main` c24e00e (#201).
- Checks: `quality` ✅ `push-advisory` ✅ `secrets-scan` ✅ `CodeRabbit` ✅ (approved, no findings).

## The bug
GT "1:NE" phantom-dispatch: a retired seat (`rank='retired'`) still carrying a
stale `golden_throne` binding was re-armed/resumed into its defunct old pane.
This is the permanent CODE half (half-1 marker-sweep of existing DB rows is a
separate, data-only owner — untouched here).

## Fix combination landed
**Reader filters (core) — exclude `rank='retired'` wherever a GT binding drives
dispatch/resume/keepalive:**
- `main.py` `recover_recent_stopped_golden_throne_timers` query (the primary phantom source)
- `main.py` `schedule_golden_throne_followup` (central arm gate — covers stop-hook endpoint + recovery)
- `main.py` `golden_throne_followup` fire path (fails closed before rubric read/dispatch)
- `routes/hooks.py` `handle_stop` morning-keepalive sync-mode arm
- `main.py` `GET /api/legion/{legion}/synced-session`
- `main.py` `GET /api/golden-throne/timers` diagnostic (consistency)

**Source hygiene — `db_schema.py` retire triggers null `golden_throne`:**
- dedicated `AFTER UPDATE OF rank` trigger nulls the retiring row's own marker
- `golden_throne=NULL` folded into the chapter-children retire cascade
- NB: recovery intentionally resumes `status='stopped'` (non-retired) GT sessions,
  so the liveness gate is `rank != 'retired'`, NOT `status != 'stopped'`.

**Dropped-table:** no runtime path reads dropped `claude_instances` (only
archive/restore in db_schema); stale comment corrected; static guard test added.
`_dispatch_custodes_intervention` already safe (resolves via
`resolve_live_persona_instance`, excludes retired+stopped).

## Verification
- New RED→GREEN tests in `tests/test_gt_timer_liveness.py`,
  `tests/test_morning_keepalive_gate.py`, `tests/test_no_dropped_table_readers.py`.
- Full token-api suite: **763 passed, 6 xfailed, 2 xpassed, 0 failed**.
- Dev-server live-test (real app code, isolated dev DB, NOT live runtime): recovery
  does not re-arm a retired seat, fire path does not dispatch, `/synced-session`
  returns None, retire trigger nulls marker — all PASS.

## Next action (FG)
Merge #203 when sequencing the batched deploy. Merge → CD → live token-api restart.
