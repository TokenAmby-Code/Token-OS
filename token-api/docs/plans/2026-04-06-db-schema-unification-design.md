# DB Schema Unification Design

**Date**: 2026-04-06
**Status**: Implemented locally, Mac verification pending
**Project**: Token-API

## Problem

`token-api` currently has multiple schema owners:

- `main.py:init_db()` runs on FastAPI startup
- `init_db.py:init_database()` is used for standalone bootstrap and tests

They are overlapping but not identical.

Concrete drift already observed:

- live `claude_instances` contains `is_subagent` and `spawner`
- `init_db.py` migrates those columns
- `main.py:init_db()` does not add those columns in its migration block
- `handle_session_start()` in `main.py` inserts both columns unconditionally

This means bootstrap correctness depends on which initializer happened to run.

That is not acceptable for a stateful orchestration system.

## Goal

There must be exactly one authoritative schema and migration definition for the SQLite database.

All entrypoints should call into that same definition:

- FastAPI startup
- standalone `python3 init_db.py`
- tests
- any future admin or repair scripts

## Non-Goals

- No full Alembic-style migration framework in this pass
- No redesign of the data model itself
- No route-level refactor as part of schema unification

## Recommendation

Create a single shared schema module and make both current entrypoints thin wrappers.

Suggested structure:

```text
token-api/
  db_schema.py        # canonical schema + migrations
  init_db.py          # thin sync wrapper for manual/test bootstrap
  main.py             # imports async wrapper for startup
```

The shared module should own:

- table creation SQL
- additive migrations
- index creation
- seed data
- one-shot data backfills tied to schema version changes

## Canonical API Shape

Suggested module interface:

```python
async def init_db_async(db_path: Path) -> None:
    ...

def init_db_sync(db_path: Path) -> None:
    ...
```

Internally, both should call shared helpers instead of duplicating SQL blocks.

Example helper layout:

```python
def apply_schema_sql(execute, fetchall, commit):
    ...

def migrate_claude_instances(execute, fetchall):
    ...

def migrate_session_documents(execute, fetchall):
    ...

def seed_devices(execute):
    ...
```

The point is not elegance. The point is one migration list.

## Why Not Keep `main.py` As Owner

Because `main.py` is already overloaded and hard to verify.

If schema truth stays embedded there:

- tests still need a separate sync path
- manual bootstrap still wants a script
- future drift is likely to recur

Schema ownership should move out of the monolith, not deeper into it.

## Why Not Keep `init_db.py` As Entire Owner Without Extraction

Because FastAPI startup still needs an async-safe init path.

If `main.py` shells out or re-implements logic around `init_db.py`, the duplication risk remains.

The safe design is:

- shared schema module is authoritative
- `init_db.py` is a sync wrapper around it
- `main.py` startup uses the async wrapper around it

## Scope of First Unification Pass

First pass should be narrow and mechanical.

### 1. Extract shared schema/migration helpers

Move all schema creation and migration logic into a new module.

At minimum this must cover:

- `claude_instances`
- `devices`
- `events`
- `scheduled_tasks`
- `task_executions`
- `task_locks`
- `audio_proxy_state`
- timer tables
- `agent_state`
- `guard_runs`
- `session_documents`
- `primarch_session_docs`
- `primarchs`
- `habits`
- `habit_completions`
- any cron engine table setup currently called during init

### 2. Make both current initializers call the same code

- `init_db.py:init_database()` becomes thin
- `main.py:init_db()` becomes thin or is deleted in favor of imported function

### 3. Verify parity against live Mac schema

At minimum compare:

- `PRAGMA table_info(claude_instances)`
- `PRAGMA table_info(session_documents)`
- `PRAGMA table_info(events)`
- indexes on `claude_instances`, `events`, and session-doc linkage tables

### 4. Update tests

Tests should initialize through the same shared schema owner.

Any test that currently calls both sync and async initializers should be simplified so duplication stops being normalized.

## Acceptance Criteria

The first pass is complete when all of the following are true:

1. there is one file that defines the schema and migrations
2. `main.py` startup no longer contains an independent schema definition
3. `init_db.py` no longer contains an independent schema definition
4. a fresh DB initialized through either path has identical schema
5. existing Mac DB startup remains non-destructive and idempotent

## Verification Plan

### Local / WSL

- initialize a fresh temp DB through the sync path
- initialize a fresh temp DB through the async path
- diff `.schema`
- diff `PRAGMA table_info` for key tables

### Mac

Use `ssh mini` or the repo wrapper `cli-tools/bin/ssh-mac`.

Recommended checks:

```bash
ssh mini "sqlite3 ~/.claude/agents.db '.schema claude_instances'"
ssh mini "sqlite3 ~/.claude/agents.db '.schema session_documents'"
ssh mini "sqlite3 ~/.claude/agents.db 'pragma table_info(claude_instances);'"
```

Then restart `token-api` and confirm:

- startup succeeds
- `GET /health` succeeds
- new sessions still register successfully

## Local Implementation Note

Implemented in this repo as:

- `token-api/db_schema.py` as the canonical schema owner
- `token-api/init_db.py` as a thin sync wrapper
- `token-api/main.py:init_db()` as a thin async wrapper

Local verification completed:

- full fresh-db `.schema` diff matches between sync and async entrypoints
- `tests/test_legion_synced.py` passes under `uv`

Residual issue:

- `tests/test_voice_pool.py` pure unit section passes through the linear-probe tests
- its API integration section hangs in the registration flow under test harness execution
- this appears separate from schema parity and should be checked independently

## Risk Notes

The danger is not just schema mismatch. It is hidden data assumptions in lifecycle code.

Example:
- `handle_session_start()` assumes `is_subagent` and `spawner`
- stop logic assumes `input_lock`, `instance_type`, `synced`, and other later columns

So the unification pass must preserve all live columns, not just "what the original create table had."

## Recommended Order Relative to Session-Doc Resolver

Do schema unification first.

Reason:
- the resolver work is already touching schema-adjacent lifecycle code
- landing resolver logic on top of split schema ownership compounds risk

After schema unification, the resolver patch becomes substantially safer.

## Agent Dispatch

This is close to being splittable, but only after the contract is accepted.

Good future split:

- Worker A: extract shared schema module and convert wrappers
- Worker B: build parity tests and fresh-DB verification script

Do not dispatch both until the module boundary is agreed, otherwise each worker will invent a different ownership model.
