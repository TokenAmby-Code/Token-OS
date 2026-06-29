---
name: golden-throne-close
description: Golden Throne closure step. Use to victory-ack a completed or skipped rubric, or disable Golden Throne for a misclassified thread by setting the instance one_off.
---

# Golden Throne Close

Close only after resolving the instance/doc and updating the session doc for this cycle.

## Victory Ack

If every victory criterion is complete or explicitly skipped with justification:

```bash
victory-ack --doc-id <doc_id> "All victory criteria complete or justified skipped: <specific proof>."
```

For dogfood validation only, call the API dry-run form directly; it must not be used as the final closure:

```bash
curl -s -X POST "$TOKEN_API_URL/api/session-docs/<doc_id>/victory-ack"   -H "Content-Type: application/json"   -d '{"reason":"dry-run validation only","dry_run":true}'
```

Do not merely say “victory” in-thread.

## Disable Wrong Thread

If Golden Throne is wrong for this thread (no linked doc, no actionable victory rubric, wrong instance, or intentionally one-off conversation), disable it by setting the instance type to `one_off`:

```bash
curl -s -X PATCH "$TOKEN_API_URL/api/instances/<instance_id>/type"   -H "Content-Type: application/json"   -d '{"instance_type":"one_off"}'
```

If the API expects a different field name, inspect the endpoint or use the local helper; do not patch SQLite directly.

## Final Report

Report the closure action, doc id, reason, and evidence. If closure failed, escalate the API error as the blocker.
