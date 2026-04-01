---
name: vault-canon
description: "Use when you have an insight, decision, or correction significant enough to write directly to the Obsidian vault. Astartes+ only. The vault is canon — earn your place in it."
---

# Vault Canon — The Earned Write

The vault is institutional memory. Writing directly to it is not routine — it's an act of significance. This skill is for Claude agents (Astartes+) who have something that belongs in the canon.

## When to Use

You've discovered something that future agents need to find in the vault:
- **A hard decision** — you weighed real trade-offs and chose. The reasoning matters.
- **A significant correction** — something in the vault is wrong and you've confirmed it.
- **A pattern or insight** — you've identified something that will save real pain.
- **An architectural truth** — discovered through implementation, not speculation.

## When NOT to Use

- Progress updates → session doc Activity Log
- Tentative ideas → session doc Decisions section (let the Administratum evaluate)
- Routine documentation → session doc
- Anything you're not confident about → session doc

**If you're unsure whether it's earned, it isn't.** Write it to the session doc. The Administratum will promote it later if it deserves to be in the vault.

## Process

### 1. Declare Intent

State clearly what you're writing and why it's earned. This isn't bureaucracy — it's the moment of conscious decision. You're choosing to alter the institutional record.

### 2. Determine Target

**Updating an existing note:**
```bash
# Search for the note you want to update
obsidian vault=<name> search query="<topic>"

# Read it to understand current content
obsidian vault=<name> read path="<path>.md"
```

**Creating a new note:**
- Follow the vault's naming conventions and folder structure
- Check the vault's CLAUDE.md for folder/tag guidelines
- New notes go in the appropriate workspace folder

### 3. Structure the Write

**For appending to existing notes:**
```bash
obsidian vault=<name> append path="<path>.md" content="

## <Section Title>

<Your content here>

> *Contributed by [instance-name], [date]. Context: [why this was written]*
"
```

**For creating new notes:**
```bash
obsidian vault=<name> create path="<folder>/<note-name>.md" content="---
title: <Title>
type: reference|planning|note
created: YYYY-MM-DD
status: active
workspace: <appropriate workspace>
tags: [relevant, tags]
related_session_docs: [Sessions/YYYY-MM-DD-session-name]
---

# <Title>

<Content>

> *Contributed by [instance-name], [date]. Context: [why this was written]*
"
```

### 4. Update Provenance

If the target note already exists, update its `related_session_docs` frontmatter to include your session doc:

```bash
obsidian vault=<name> property:set path="<path>.md" property="related_session_docs" value="[existing-docs, Sessions/your-session-doc]" type=list
```

### 5. Note in Session Doc

Record what you wrote in your session doc's `## Decisions` section so the Activity Log captures it:

```
Wrote to vault: [[Meta/Vault Mind Architecture]] — documented the identity model and write hierarchy.
```

## Frontmatter Standards

All vault-canon writes must include:
- `related_session_docs` — which session doc(s) this came from
- Standard vault fields per the vault's CLAUDE.md (title, type, created, status, tags)
- Attribution line in content body

## Quality Over Quantity

A vault with 10 hard-earned notes is worth more than a vault with 100 routine dumps. The Administratum handles volume. You handle significance.

MiniMax agents (Imperial Guard) never invoke this skill. They write to session docs only. This ceremony is for agents with the judgment to know what belongs in the canon.
---
name: vault-canon
description: "Use when you have an insight, decision, or correction significant enough to write directly to the Obsidian vault. Astartes+ only. The vault is canon — earn your place in it."
---

# Vault Canon — The Earned Write

The vault is institutional memory. Writing directly to it is not routine — it's an act of significance. This skill is for Claude agents (Astartes+) who have something that belongs in the canon.

## When to Use

You've discovered something that future agents need to find in the vault:
- **A hard decision** — you weighed real trade-offs and chose. The reasoning matters.
- **A significant correction** — something in the vault is wrong and you've confirmed it.
- **A pattern or insight** — you've identified something that will save real pain.
- **An architectural truth** — discovered through implementation, not speculation.

## When NOT to Use

- Progress updates → session doc Activity Log
- Tentative ideas → session doc Decisions section (let the Administratum evaluate)
- Routine documentation → session doc
- Anything you're not confident about → session doc

**If you're unsure whether it's earned, it isn't.** Write it to the session doc. The Administratum will promote it later if it deserves to be in the vault.

## Process

### 1. Declare Intent

State clearly what you're writing and why it's earned. This isn't bureaucracy — it's the moment of conscious decision. You're choosing to alter the institutional record.

### 2. Determine Target

**Updating an existing note:**
```bash
# Search for the note you want to update
obsidian vault=<name> search query="<topic>"

# Read it to understand current content
obsidian vault=<name> read path="<path>.md"
```

**Creating a new note:**
- Follow the vault's naming conventions and folder structure
- Check the vault's CLAUDE.md for folder/tag guidelines
- New notes go in the appropriate workspace folder

### 3. Structure the Write

**For appending to existing notes:**
```bash
obsidian vault=<name> append path="<path>.md" content="

## <Section Title>

<Your content here>

> *Contributed by [instance-name], [date]. Context: [why this was written]*
"
```

**For creating new notes:**
```bash
obsidian vault=<name> create path="<folder>/<note-name>.md" content="---
title: <Title>
type: reference|planning|note
created: YYYY-MM-DD
status: active
workspace: <appropriate workspace>
tags: [relevant, tags]
related_session_docs: [Sessions/YYYY-MM-DD-session-name]
---

# <Title>

<Content>

> *Contributed by [instance-name], [date]. Context: [why this was written]*
"
```

### 4. Update Provenance

If the target note already exists, update its `related_session_docs` frontmatter to include your session doc:

```bash
obsidian vault=<name> property:set path="<path>.md" property="related_session_docs" value="[existing-docs, Sessions/your-session-doc]" type=list
```

### 5. Note in Session Doc

Record what you wrote in your session doc's `## Decisions` section so the Activity Log captures it:

```
Wrote to vault: [[Meta/Vault Mind Architecture]] — documented the identity model and write hierarchy.
```

## Frontmatter Standards

All vault-canon writes must include:
- `related_session_docs` — which session doc(s) this came from
- Standard vault fields per the vault's CLAUDE.md (title, type, created, status, tags)
- Attribution line in content body

## Quality Over Quantity

A vault with 10 hard-earned notes is worth more than a vault with 100 routine dumps. The Administratum handles volume. You handle significance.

MiniMax agents (Imperial Guard) never invoke this skill. They write to session docs only. This ceremony is for agents with the judgment to know what belongs in the canon.
