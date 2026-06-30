---
name: preplan
description: "Explicit-only last persistence handoff before plan mode, transplant, or compaction. Use only when invoked as /preplan in Claude or $preplan in Codex to update docs/artifacts, summarize state, identify decisions, then stop for the user or harness next command."
---

# Preplan

Preplan is the last persistence chance before plan mode, transplant, compaction, or a harness-issued next command. Preserve state, then stop. Do not implement.

## Contract

- Do not implement product/code changes.
- Do not run destructive commands.
- Update sanctioned session docs, vault notes, or artifacts only when needed to preserve planning context.
- End with a concise handoff whose final line starts with `preplan complete:`.

## Process

1. Resolve the current instance and linked session doc when Token-API is available.
2. Read or verify the relevant session doc/artifact.
3. Merge missing current-state, decision, blocker, validation, or next-step context.
4. Summarize objective, known state, likely files/systems, completed investigation, remaining decisions, and recommended next planning focus.
5. Stop. Expect the user or harness to issue the next plan/transplant/execute command.

## Output

```markdown
- State: ...
- Remaining decisions: ...
- Recommended plan focus: ...
preplan complete: <one-sentence handoff>
```
