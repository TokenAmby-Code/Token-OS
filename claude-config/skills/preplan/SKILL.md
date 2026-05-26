---
name: preplan
description: "Explicit-only pre-implementation planning handoff. Use only when invoked as /preplan in Claude or $preplan in Codex to update/verify session docs, summarize current state, identify remaining decisions, and stop before implementation."
---

# Preplan

Prepare the next planning turn without implementing code.

## Contract

- Do not implement product/code changes during preplan.
- Do not run destructive commands.
- Only write sanctioned session-doc/vault updates when needed to preserve planning context.
- End with a concise handoff whose final line starts with `preplan complete:`.

## Process

1. Resolve the current instance and linked session doc when Token-API tools are available.
2. Read or verify the relevant session doc and nearby vault context.
3. Update the session doc only if important current-state, decision, or blocker context is missing.
4. Summarize:
   - current objective and known state,
   - files/systems likely involved,
   - completed investigation,
   - remaining decisions or risks,
   - recommended next planning focus.
5. Stop naturally. The operator hook may submit `/plan create the plan` after this response.

## Output

Keep the final response short. Use this shape:

```markdown
- State: ...
- Remaining decisions: ...
- Recommended plan focus: ...
preplan complete: <one-sentence handoff>
```
