---
name: custodes
description: Worker/overseer routing guide for reporting to or about Custodes, checking Custodes singleton identity, daily-note ownership, enforcement escalation, and deciding whether to dispatch, brief, or escalate to Custodes.
---

# Custodes

Use this skill to route work **to or about Custodes**. It is not a self-manual for becoming Custodes and not permission to patch Custodes identity.

Custodes is the Emperor-facing overseer singleton: first contact, escalation tier, accountability/enforcement seat, daily-note semantic owner, and dispatch designator.

## Route to Custodes When

- The task concerns daily-note meaning, Emperor-facing accountability, enforcement posture, or operator-facing escalation.
- A worker needs a scope/gate decision that its commander cannot answer.
- A singleton identity, pane, or registry invariant appears wrong and needs harness/registry escalation.
- A result must be reported to Custodes rather than implemented from the current pane.

## Routing Surfaces

- Persona note: `$IMPERIUM/Imperium-ENV/Personas/Custodes.md`.
- Related skills: `$daily-note`, `$dispatch`, `$session-update`, `$vault-update`.
- Use `talk` for status/clarification, `brief` for structured assignment or escalation, and `$dispatch custodes` for bounded worker routing under Custodes authority.

## Identity Check

Singleton pane identity is infrastructure-owned. SessionStart/registry derive persona + rank; agents verify and report bugs, they do not self-patch.

Canonical live check: exactly one non-retired row with `persona.slug == "custodes"` and `rank != "retired"`.

```bash
curl -s "$TOKEN_API_URL/api/instances" \
  | jq '[.[] | select(.persona.slug=="custodes" and .rank!="retired")] | {count: length, row: .[0] | {id, rank, status, pane_label}}'
```

## Do Not

- Do not implement broad repo work from a Custodes pane; dispatch or use a worker.
- Do not rewrite the daily note from non-Custodes context unless explicitly assigned.
- Do not PATCH Custodes identity, legion, rank, sync, singleton binding, or registry rows locally.
- Do not compact/plan-cycle Custodes casually; protect its conversational context.
