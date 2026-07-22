---
name: persona
description: Persona-system design and debugging guide for Token-OS identity, ranks, singleton panes, Astartes chapter locks, Mechanicus workers, Black Shields, Primarchs, and static tmuxctld pane identity. Use when diagnosing persona/rank behavior, not for self-registering mid-session.
---

# Persona

Use this skill to understand or debug the persona system. Do **not** use it to self-register, self-patch, or hide registry failures.

## Authority Model

- Identity is infrastructure-owned: SessionStart, dispatcher, registry, and tmuxctld pane stamps establish persona + rank.
- Agents verify and report mismatches; they do not PATCH their own DB rows.
- Persona files provide behavior/context. Registry rows provide fleet-visible identity.
- Rank doctrine constrains authority more strongly than voice/flavor.

## Identity Axes

- **Persona:** named behavior/context, usually from `$IMPERIUM_VAULT/Personas/<Name>.md`.
- **Rank:** authority class such as Aspirant, Astartes, Overseer, Primarch, or retired.
- **Legion/chapter/lock:** routing/voice constraints for Astartes-style personas; chapter locks should not override registry truth.
- **Singleton pane:** static persona seats such as Custodes, Fabricator-General, Administratum, Pax, Orchestrator, or Malcador. These are protected infrastructure seats.
- **Mechanicus worker:** dispatched implementation/investigation worker; not an overseer unless explicitly promoted/deputized.
- **Black Shield:** special/unaffiliated persona state; handle through documented registry/rank rules, not ad-hoc mutation.
- **Primarch:** high-authority persona/rank path for architecture/doctrine domains.

## Debug Procedure

1. Read the relevant persona and rank files.
2. Resolve the live row through Token-API and inspect `persona.slug`, `rank`, `pane_label`, status, commander, and session doc.
3. Compare registry identity to tmuxctld/static pane labels and SessionStart expectations.
4. If a protected singleton is wrong, report a harness/SessionStart/tmuxctld invariant failure; do not repair by PATCH.
5. If an ad-hoc worker has wrong identity, fix the launcher/dispatch/persona-seat path that created it.

Safe inspection:

```bash
curl -s "$TOKEN_API_URL/api/instances" \
  | jq '.[] | {id, persona: .persona.slug, rank, status, pane_label, commander_instance_id, session_doc_id}'
```

## Civic Mirror & Drift

The civic singleton seats are **manual redubs** of their personal-side counterparts, hosted in
`Pax-ENV/Personas/`:

- **Pax** ⇄ **Custodes** (`Personas/Custodes.md`)
- **Orchestrator** ⇄ **Fabricator-General** (`Personas/Fabricator-General.md`)

They are produced by hand with civic naming swaps (`custodes`→`pax`, `fabricator-general`→
`orchestrator`, `mechanicus`→`civic`, Imperium framing→un-themed civic, daily note→civic record).
They are **not** auto-generated and they **deliberately diverge** where civic behavior differs —
e.g. **Pax has no enforcement seat** and is silent/day-job-scoped.

**Drift rule:** whenever you edit a personal-side persona doc (Custodes / Fabricator-General),
**consider a fast-follow patch to its civic mirror** (Pax / Orchestrator) so they do not drift.
Treat the mirror update as part of the same change unless the edit is purely personal-side behavior.

**Future option (not built):** have the runtime doc-staple process — which already staples persona +
rank files together — also hot-read the personal-side file and apply the key-term swaps at
compile/staple time, so the civic seats can't drift. This is **deferred**: a naive text-swap is
unsafe because of the deliberate behavioral deltas above (it would re-introduce enforcement into
Pax). Any such system must subtract the civic-specific deltas, not just rename tokens.

**Engine note:** `PersonaSeed` carries no engine field; engine is chosen at launch/dispatch
(`dispatch` defaults to `--engine claude`). Per-seat engine defaults (e.g. FG + Orchestrator on
Codex) are set on the seat's launch/respawn lever, not in the persona doc or seed.

## Do Not

- Do not invoke `/persona <name>` as a local self-registration escape hatch.
- Do not PATCH your own identity, rank, legion, singleton binding, sync mode, or commander row.
- Do not confuse `synced`/runtime mode with persona identity.
- Do not let a persona voice file override rank, dispatcher authority, or singleton constraints.
