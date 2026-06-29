---
name: golden-throne-rubric
description: Golden Throne rubric inspection step. Use to identify unmet victory criteria, prioritize the named invocation condition, and decide whether completion, progress, skip, or escalation is valid.
---

# Golden Throne Rubric

Inspect the session doc's victory rubric after `$golden-throne-resolve`.

1. List every condition and mark it complete, incomplete, skipped, or unclear.
2. Prioritize the condition named in the invocation text, but do not ignore other unmet criteria.
3. Treat evidence as required. A claim in chat is not completion unless the doc or external proof supports it.
4. If a criterion is genuinely inapplicable, record `<rubric_key>_skip` with a concrete justification in the doc body. Do not silently delete or reinterpret it.
5. If completion depends on merge/deploy/live verification, inspect the actual PR, CI, runtime HEAD, health endpoint, or named proof surface.

Output a next action: make progress, validate and mark complete, add justified skip, escalate blocker, victory-ack, or disable Golden Throne as wrong for the thread.
