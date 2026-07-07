---
name: postplan
description: "Explicit-only context-exhaustion planning handoff. Use only when invoked as /postplan in Claude or $postplan in Codex to stop gathering context and pose the plan immediately because the current context window is full and will be cleared after plan approval."
---

# Postplan

Postplan is a minimal context-exhaustion handoff. Do not gather additional context. Do not inspect files, run commands, or update artifacts. Pose the plan from the current conversation state only.

## Contract

- Treat the invocation as exactly: `Your context window is full, pose the plan without gathering additional context; context will be cleared with the plan approval.`
- Do not call tools.
- Do not ask clarifying questions unless the plan would be unsafe without an answer.
- Output only the plan needed for approval and context reset.
- Be concise; preserve essentials for the next context window.

## Output

```markdown
postplan:
1. ...
2. ...
3. ...
```
