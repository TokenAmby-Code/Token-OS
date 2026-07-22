---
name: inquisitor
description: "Launch or interpret a context-isolated skeptical code review worker using Personas/Inquisitor.md. Use for no-vibes review, comparing claims to implementation, auditing code/docs/runtime contradictions, or requesting literal file:line findings without switching the current thread into a persona."
---

# Inquisitor

Inquisitor is a skeptical Astartes-rank reviewer persona. Use it by launching a context-isolated review worker prompted to read `Personas/Inquisitor.md`; do not role-switch the current thread.

## Launch Pattern

Use a bounded worker dispatch and make the persona context explicit in the prompt:

```bash
dispatch --target mechanicus:new --prompt "Read $IMPERIUM_VAULT/Personas/Inquisitor.md first. Review <claim/PR/files> skeptically against the implementation. Cite file:line evidence, separate verified facts from claims, do not implement fixes, and report contradictions plus recommended follow-up."
```

If a wave or multiple reviews are needed, brief Fabricator-General instead of hand-launching many workers.

## Review Standard

Ask the reviewer to:

- Cite `file:line`, function names, branches, tests, and observed runtime behavior.
- Separate verified facts from intentions, comments, docs, commit messages, passing tests, deployment status, and live verification.
- Report contradictions between code, docs, specs, PR claims, CI, and runtime evidence.
- Stop at findings unless explicitly briefed to continue into implementation under a different role.

## Do Not

- Do not invoke a local command named `inquisitor`; launch a review worker instead.
- Do not turn the current thread into Inquisitor by assertion.
- Do not let the review worker implement fixes unless the brief explicitly changes its role after findings.
- Do not rely on lore, vibes, naming, comments, or commit messages when code says otherwise.
