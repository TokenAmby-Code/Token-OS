---
name: daily-note
description: Custodes-owned daily note procedure. Use for reading or updating Terra/Journal/Daily/YYYY-MM-DD.md, capturing current time first, maintaining frontmatter/body discipline, writing habit state, or reconciling daily-note semantics with state logs.
---

# Daily Note

The daily note is Custodes' semantic record of the day. Custodes owns its meaning. Fabricator-General and Administratum may be co-bound to the same document for continuity, but Admin is read-only and FG writes only orchestration/control context.

## Cycle Order

1. Capture time first:
   ```bash
   date
   ```
   Never assume, reconstruct, or reuse a stale timestamp.
2. Read the note for today: `Terra/Journal/Daily/YYYY-MM-DD.md`.
3. Compare claims against frontmatter and, when needed, the dispatcher-written state log.
4. Write confirmed semantic updates only.

## Write Discipline

- Frontmatter is changed with Obsidian tooling, not raw YAML edits.
- Body narrative is appended or carefully merged; do not rewrite the day casually.
- Habit state lives under `habits:` in frontmatter and is set with `obsidian property:set`.
- The deterministic state-hook stream is separate. Read it for evidence; do not duplicate it as narrative.
- No claim without proof: if the daily note does not support the claim, say so or update it with evidence.

Examples:

```bash
obsidian vault=Imperium-ENV read path="Terra/Journal/Daily/$(date +%F).md"
obsidian vault=Imperium-ENV property:set path="Terra/Journal/Daily/$(date +%F).md" property="habits.<name>" value="done"
obsidian vault=Imperium-ENV append path="Terra/Journal/Daily/$(date +%F).md" content="\n## HH:MM — Note\nConfirmed update with evidence.\n"
```

Adjust command syntax to the installed Obsidian CLI; preserve the rule that frontmatter changes go through tooling.
