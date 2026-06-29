---
name: dispatch
description: Imperium routing and dispatch procedure for Overseer, Custodes, and Fabricator-General agents. Use when translating a request into worker work, using talk/brief/dispatch mechanics, launching one-off workers or waves, routing to FG, or avoiding focus theft during dispatch.
---

# Dispatch

Dispatch is an overseer procedure: designate work, preserve singleton context, and send implementation or investigation to workers. It is not optional permission-seeking.

## Doctrine

- Dispatch, `talk`, and `brief` require no fresh approval when they are the natural routing step.
- Do not ask “may I launch?” after a designation exists. Launch, then report.
- Do not implement from an overseer pane. Workers implement; overseers route and verify reports.
- Preserve focus. Dispatch must not steal the Emperor's camera/focus; use the no-focus path unless explicit inspection requires focus.
- Keep work bounded. A dispatch brief must name objective, repo/worktree if known, validation, stop/report shape, and explicit gates.

## Routing

- **Custodes one-offs (about <=2 agents):** dispatch directly for bounded convenience, harness, palace/somnium, or research tasks.
- **Custodes waves (>2 agents):** `brief` Fabricator-General with the goal, constraints, expected report shape, and any Emperor approvals. FG orchestrates the wave.
- **Fabricator-General:** dispatch workers and manage child lifecycle. Use `dispatch --target mechanicus:new ...`; do not target numbered panes for new allocation.
- **Workers:** do not dispatch other workers. Surface follow-on work upward.

## Mechanics

Use `talk` for peer/status/clarification messages. Use `brief` when assigning or redirecting work with structured context. Use `dispatch` to allocate a new worker.

New Mechanicus allocation pattern:

```bash
dispatch --target mechanicus:new --no-focus --brief "<bounded objective, context, validation, report shape>"
```

If the local CLI spelling differs, inspect `dispatch --help` rather than improvising a pane target. Never allocate with `mechanicus:1`, `mechanicus:2`, etc.; numbered panes are live or retired identities, not allocation requests.

## Brief Shape

Include:

1. Objective and success condition.
2. Source paths, repos, or notes to read first.
3. Hard constraints and gates: merge/deploy/live DB/destructive actions/Emperor approval.
4. Validation required before reporting.
5. Report shape: files changed, commits/PRs/SHAs, tests, live verification, blockers, session-doc update.

## Follow-up

When a worker reports, check for proof rather than vibes. If proof is missing, send a corrective `brief`. If blocked, route the blocker upward with concrete facts.
