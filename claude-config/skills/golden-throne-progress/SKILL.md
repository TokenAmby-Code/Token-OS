---
name: golden-throne-progress
description: Golden Throne progress step. Use to make measurable progress on an unmet victory criterion, validate it, or escalate a concrete blocker while preventing repeated no-op wake cycles.
---

# Golden Throne Progress

Each Golden Throne wake must change state. Do not loop.

Allowed outcomes:

- Make measurable progress on the unmet criterion.
- Validate existing work and update the session doc with proof.
- Add a justified skip for an inapplicable criterion.
- Escalate a concrete blocker through the configured notify/talk/brief path.
- Close with victory-ack or disable the thread via `$golden-throne-close`.

## Rules

- Work on the named unmet condition first unless another condition blocks it.
- Validate before changing rubric state.
- Do not repeat the prior wake's exact action if it failed to change state.
- If blocked, name the blocker, attempted command/action, observed failure, and the decision or access needed.
- Update the session doc this cycle, even if the outcome is escalation or disablement.
