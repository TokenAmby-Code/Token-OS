---
name: golden-throne-sop
description: Golden Throne follow-up procedure for resuming a session with unmet victory rubric criteria. Use when invoked as $golden-throne-sop or /golden-throne-sop, especially with text like `victory condition "needs tests passing" is unmet`, to inspect the linked session doc, address the specific missing criterion, escalate if blocked, and perform the proper victory-ack or GT-disable transition instead of claiming completion in-thread.
---

# Golden Throne SOP

You were resumed by Golden Throne because a linked session document has an unmet victory rubric condition. Treat the invocation arguments as the trigger label, not as the whole task.

## Procedure

1. Read your linked session document. If the doc id is not obvious, resolve this instance through Token-API and read its `session_doc_id`.
2. Inspect the `victory` rubric and identify every unmet condition, prioritizing the condition named in the invocation.
3. If clear, make measurable progress on the unmet condition. Validate the work before changing rubric state.
4. If blocked, escalate with a concrete blocker through the configured notify/escalation path; do not silently stop.
5. If a condition is genuinely inapplicable, add it to `<rubric_key>_skip` with justification in the doc body.
6. Update the session document with what happened this cycle.
7. If all rubric conditions are now complete or skipped, use `victory-ack` / `POST $TOKEN_API_URL/api/session-docs/<doc_id>/victory-ack` with a specific reason. Do not merely write “victory” in-thread.
8. If Golden Throne is wrong for this thread, disable it by setting the instance to `one_off` via `PATCH $TOKEN_API_URL/api/instances/<instance_id>/type`.

Do not allow a Sisyphus loop: each wake must either make measurable progress, escalate, mark justified skip state, victory-ack, or disable Golden Throne for the thread.
