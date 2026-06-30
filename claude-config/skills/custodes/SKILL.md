---
name: custodes
description: Custodes routing guide for reporting to Custodes, escalating operator/accountability issues, checking Custodes singleton identity, daily-note ownership, enforcement routing, and choosing talk, brief, or dispatch paths.
---

# Custodes

Custodes is the Emperor-facing overseer singleton: first contact, escalation tier, accountability/enforcement seat, daily-note semantic owner, and dispatch designator.

## Route to Custodes When

- Daily-note meaning, Emperor-facing accountability, enforcement posture, or operator escalation is involved.
- A worker or overseer needs a scope/gate decision from the top command surface.
- Singleton identity, pane, or registry invariants look wrong.
- A result should reach the Emperor through the Custodes surface.

## Communicate with Custodes

Use `talk` for short status, questions, or heads-up messages:

```bash
talk --pane council:custodes "<short status, question, or escalation>"
```

Use `brief` for structured reports, assignments, or decisions needing durable context:

```bash
brief --pane council:custodes "<objective/status, evidence, blocker or decision needed, next action>"
```

If `talk`/`brief` returns `unverified` with “bytes may have issued,” do not blind-retry. Report the uncertainty or use a new clearly-deduplicated message.

## Context

- Persona: `$IMPERIUM/Imperium-ENV/Personas/Custodes.md`.
- Related procedures: `$daily-note`, `$dispatch`, `$session-update`, `$vault-update`.
- Dispatch posture: Custodes directly handles bounded one-offs; coordinated waves go to Fabricator-General.

## Identity Check

Singleton pane identity is infrastructure-owned. SessionStart/registry derive persona + rank; agents verify and report mismatches.

Canonical live check: exactly one non-retired row with `persona.slug == "custodes"` and `rank != "retired"`.

```bash
curl -s "$TOKEN_API_URL/api/instances" \
  | jq '[.[] | select(.persona.slug=="custodes" and .rank!="retired")] | {count: length, row: .[0] | {id, rank, status, pane_label}}'
```

## Guardrails

- Keep broad repo work in worker panes.
- Write daily-note semantics from Custodes context or explicit Custodes assignment.
- Report identity/registry mismatches upward; leave registry mutation to infrastructure.
- Preserve Custodes conversational context.
