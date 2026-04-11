# Session Doc Resolution Design

**Date**: 2026-04-06
**Status**: Draft
**Project**: Token-API

## Summary

Session-doc assignment should move from "always auto-create a fresh doc for top-level sessions" to:

- every top-level instance must have exactly one `session_doc_id`
- prefer the most relevant existing active doc
- create a new doc only when no good candidate exists

This logic belongs at session start, not in the stop-hook critical path.

## Current Behavior

Today `handle_session_start()` does this for top-level sessions:

- preserve doc linkage for supplant/transplant paths
- preserve active primarch-linked docs
- otherwise auto-create a new doc for interactive sessions based on `working_dir` basename + date
- otherwise auto-create/reuse per-cron-job docs for cron agents

That guarantees coverage, but it over-produces session docs and encodes a mostly per-session model instead of an idea/workstream model.

## Desired Invariant

For every top-level instance:

1. the instance must end session start with a non-null `session_doc_id`
2. if a relevant active doc already exists, the instance should join it
3. if no relevant active doc exists, create a new one

Subagents remain excluded from auto-creation.

## Non-Goals

- No LLM similarity call in the stop hook
- No background auto-merge as part of session start
- No attempt to perfectly infer semantic intent from arbitrary transcripts in v1
- No large schema migration required for first rollout

## Placement

Add a helper on the session-start path:

```python
async def resolve_or_create_session_doc(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    working_dir: str,
    origin_type: str,
    is_subagent: bool,
    primarch_name: str | None,
    existing_session_doc_id: int | None,
    explicit_session_doc_id: int | None = None,
    explicit_session_doc_title: str | None = None,
    cron_job_id: str | None = None,
    tab_name: str | None = None,
) -> tuple[int | None, str]:
    """
    Returns (session_doc_id, resolution_reason)
    resolution_reason examples:
      "preserved_supplant"
      "primarch_active_doc"
      "explicit_assignment"
      "same_worktree_recent"
      "same_project_recent"
      "same_title_stem_recent"
      "created_interactive"
      "created_cron"
    """
```

Call it from `handle_session_start()` in place of the current interactive auto-create block.

## Resolution Order

Resolution should be deterministic and short-circuit in this order.

### 1. Preserve already-known linkage

Use an existing doc immediately when available from:

- supplant/transplant flow
- prior preserved instance linkage
- explicit user assignment

This should win over heuristics.

### 2. Primarch active-doc linkage

If `primarch_name` has an active mapping in `primarch_session_docs`, use that doc.

This preserves the existing primarch singleton/document behavior.

### 3. Cron-specific reuse

If `origin_type == "cron"` and `cron_job_id` is present:

- reuse active doc with same `cron_job_id`
- otherwise create a new cron doc

This preserves current cron semantics.

### 4. Worktree-local recent active-doc match

For interactive top-level sessions, first search docs already linked to recent instances in the same `working_dir`.

Candidate query source:
- `claude_instances.session_doc_id`
- `claude_instances.working_dir`
- `claude_instances.last_activity`
- `session_documents.status`

Suggested rule:
- only consider `session_documents.status = 'active'`
- only consider docs linked by top-level instances
- rank most recent linked activity first

This is the highest-signal non-explicit heuristic already available in the data model.

### 5. Project or title-stem recent match

If no same-worktree match exists, search recent active docs using deterministic heuristics:

- exact `project` match if project exists later
- exact title stem match based on `Path(working_dir).name`
- optionally a normalized tab-name/title stem match once naming is more stable

This is still heuristic, but less brittle than creating a new doc unconditionally.

### 6. Create new doc

If no candidate survives the above filters, create a new session doc.

This preserves the valuable existing assertion:
- all top-level instances have a session doc

## Candidate Ranking

V1 should use a simple, explainable score rather than opaque fuzzy matching.

Example:

- `+100` explicit/preserved linkage
- `+90` active primarch-linked doc
- `+80` same cron job
- `+70` same `working_dir` seen on a recent top-level instance
- `+40` same title stem
- `+20` same project
- `+10` recently updated

Tie-breakers:

1. active docs with currently linked non-stopped instances
2. most recent `claude_instances.last_activity`
3. most recent `session_documents.updated_at`
4. lowest doc id only as final deterministic tie-break

No LLM ranking in v1.

## Initial SQL Shape

Illustrative query for worktree-local candidates:

```sql
SELECT
    sd.id,
    sd.title,
    sd.project,
    sd.updated_at,
    MAX(ci.last_activity) AS last_instance_activity,
    SUM(CASE WHEN ci.status IN ('processing', 'idle')
              AND COALESCE(ci.is_subagent, 0) = 0 THEN 1 ELSE 0 END) AS active_top_level_links
FROM session_documents sd
JOIN claude_instances ci ON ci.session_doc_id = sd.id
WHERE sd.status = 'active'
  AND ci.working_dir = ?
GROUP BY sd.id, sd.title, sd.project, sd.updated_at
ORDER BY active_top_level_links DESC, last_instance_activity DESC, sd.updated_at DESC, sd.id DESC
LIMIT 10
```

The final implementation can break this into repository helpers rather than one huge query.

## Data Model Notes

V1 can ship without schema changes.

Likely future additions:

- `session_documents.topic_key TEXT`
- possibly `claude_instances.topic_key TEXT`

That would let the system represent the intended "idea/workstream" relationship directly instead of inferring it from path and title heuristics.

## Observability

Each resolution should log:

- `session_doc_resolution_reason`
- `session_doc_candidate_count`
- `session_doc_id`
- whether a doc was created

Recommended event:

```python
await log_event(
    "session_doc_resolved",
    instance_id=session_id,
    details={
        "doc_id": session_doc_id,
        "reason": resolution_reason,
        "created": created,
        "working_dir": working_dir,
        "origin_type": origin_type,
    },
)
```

This is necessary because doc-selection bugs will otherwise feel like "why did Claude join the wrong idea?" with no trace.

## Failure Policy

Resolver failure must not leave a top-level session without a doc.

If matching logic errors out:

- log the error
- fall back to create-new behavior
- keep session start successful

In other words, the system should degrade to the current compromise, not to null linkage.

## Tests

Minimum tests for first implementation:

1. supplant preserves prior `session_doc_id`
2. primarch-linked session reuses active primarch doc
3. cron session reuses same `cron_job_id` doc
4. interactive session reuses recent same-worktree doc
5. interactive session creates new doc when no candidate exists
6. subagent does not auto-create a doc
7. resolver exception falls back to create-new behavior

## Rollout Plan

1. Extract resolver helper without changing schema
2. Add event logging for resolution reason
3. Switch interactive session-start path to use resolver
4. Observe active-doc duplication trend before adding any semantic merge tooling

## Agent Dispatch

Not yet.

Implementation workers become useful after:

- schema ownership is assigned to one path
- resolver contract is accepted
- write scopes are split

Natural future split:

- Worker A: unify schema/repository layer for session docs and instances
- Worker B: wire resolver into `handle_session_start()` and add tests

