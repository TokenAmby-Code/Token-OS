---
name: golden-throne-sop
description: Golden Throne orchestrator for resuming a session with unmet victory rubric criteria. Use when invoked as $golden-throne-sop or /golden-throne-sop, especially with text like `victory condition "needs tests passing" is unmet`, to resolve the doc, inspect rubric, make progress or escalate, and victory-ack or disable the thread.
---

# Golden Throne SOP

You were resumed because a linked session document has an unmet victory rubric condition. Treat invocation arguments as the trigger label, not the whole task.

Run the decomposed procedure in order:

1. `$golden-throne-resolve` — resolve instance/session doc and read current victory state.
2. `$golden-throne-rubric` — identify unmet criteria, prioritizing the named condition.
3. `$golden-throne-progress` — make measurable progress, validate, skip with justification, or escalate a concrete blocker.
4. `$session-update` — record what happened this cycle.
5. `$golden-throne-close` — if all criteria are complete/skipped, call victory-ack; if Golden Throne is wrong for this thread, set the instance `one_off`.

Do not allow a Sisyphus loop. Each wake must produce measurable progress, validation, justified skip state, escalation, victory-ack, or disablement.
