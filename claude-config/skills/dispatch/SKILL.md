---
name: dispatch
description: "Imperium routing and dispatch procedure. Use for $dispatch custodes, $dispatch fabricator-general, or $dispatch worker decisions; launching bounded workers; briefing FG; using talk/brief/dispatch mechanics; preserving singleton focus; or handling the explicitly deputized worker-dispatch exception."
---

# Dispatch

Dispatch designates work, preserves singleton context, and sends implementation or investigation to the right worker tier. It is not optional permission-seeking once a designation exists.

## Forms

### `$dispatch custodes`

Use for Custodes-facing routing.

- Custodes may directly dispatch one-offs for bounded convenience, harness, palace/somnium, or research tasks involving about two agents or fewer.
- For larger waves, Custodes briefs Fabricator-General with goal, constraints, approvals, validation, and report shape.
- Custodes should not implement broad repo work from the singleton pane.

### `$dispatch fabricator-general`

Use for Fabricator-General orchestration.

- FG dispatches workers and manages child lifecycle.
- Allocate new Mechanicus workers with `mechanicus:new`; do not target numbered panes for new allocation.
- FG owns waves, worker fanout, follow-up briefs, proof review, and blocker routing.

### `$dispatch worker`

Use for worker boundaries.

- Workers implement, validate, update session docs, and report upward.
- Workers do **not** dispatch other workers by default.
- Exception: a worker may dispatch only when explicitly deputized in its brief/session/rank instructions, and only within the named scope, budget, validation, and report shape.
- If more work appears without deputized authority, surface it upward instead of spawning.

## Mechanics

Use `talk` for peer/status/clarification messages. Use `brief` for structured assignments or redirection. Use `dispatch` to allocate a new worker.

New worker allocation:

```bash
dispatch --target mechanicus:new --prompt "<bounded objective, context, validation, report shape>"
```

Dry-run validation without launching:

```bash
dispatch --target mechanicus:new --prompt "dry run validation" --dry-run
```

If local CLI spelling differs, inspect `dispatch --help`; do not improvise a pane target. Numbered panes are live or retired identities, not allocation requests.

## Brief Shape

Include:

1. Objective and success condition.
2. Source paths, repos, notes, or session docs to read first.
3. Hard constraints and gates: merge, deploy, live DB, destructive actions, Emperor approval.
4. Validation required before reporting.
5. Report shape: files changed, commits/PRs/SHAs, tests, live verification, blockers, session-doc update.
6. Whether worker-dispatch authority is explicitly deputized; absent this line, it is not.

## Follow-up

When a worker reports, check for proof rather than vibes. If proof is missing, send a corrective `brief`. If blocked, route the blocker upward with concrete facts.
