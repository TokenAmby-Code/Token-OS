---
name: postplan
description: "Explicit-only context-exhaustion planning handoff. Use only when invoked as /postplan in Claude or $postplan in Codex to stop gathering context and pose the plan immediately because the current context window is full and will be cleared after plan approval."
---

# Postplan

Postplan is a minimal context-exhaustion handoff. Do not gather additional context beyond the explicit exceptions in the contract. Do not inspect files, run commands, or update artifacts except where expressly sanctioned below. Pose the plan from the current conversation state, any previous plan file, and any allowed explore-agent returns.

## Contract

- Treat the invocation as exactly: `Your context window is full, pose the plan without gathering additional context; context will be cleared with the plan approval.`
- Read the previous plan file if one exists; this is the only sanctioned file read.
- If `postplan` is followed by a number, that number is the maximum number of explore agents you may use before posing the plan, e.g. `$postplan 2` may use up to two explore agents.
- When explore agents are used, read only their returns; do not inspect their transcripts.
- Do not call tools except as required to read the previous plan file or to launch/read allowed explore-agent returns.
- Do not ask clarifying questions unless the plan would be unsafe without an answer.
- Pose the plan using the normal harness planning semantics for the active environment.
- Be concise; preserve essentials for the next context window.
