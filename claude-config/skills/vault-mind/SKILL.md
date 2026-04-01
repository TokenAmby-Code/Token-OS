---
name: vault-mind
description: "Use on session startup when a session document exists. Reads the session doc, traverses wikilinks into the Obsidian vault, and orients the agent with identity-aware context."
---

# Vault Mind — Startup Rite

Read your session document and enter the vault. The Obsidian vault is your extended mind — the session doc is your thread of consciousness.

## When to Use

- On session startup, when you have a linked session document
- When resuming work on a task that has a session doc
- When another agent asks you to pick up a session doc

## Identity Model

**Session doc = self.** You are reading your own memory. "I decided X. I was working on Y. Let me continue."

**Vault notes = institutional.** These were written before you — by other agents, by the Emperor, by the Administratum. Read with fresh judgment. Respect the context, but verify the conclusions.

## Process

### 1. Resolve Your Session Doc

```bash
# Resolve instance ID via PID lookup, then check for linked doc
INSTANCE_ID=$(token-ping "instances" | jq -r --argjson pid "$(ps -o ppid= -p $PPID | tr -d ' ')" '[.[] | select(.pid == $pid)] | .[0].id // empty')
token-ping "instances/$INSTANCE_ID/session-doc"
```

If `session_doc_id` is null, check if you were given a doc ID or path directly. If no session doc exists and the task warrants one:
- Create new: `instance-name "<name>" --session`
- Link existing by title: `instance-name "<name>" --session "existing-doc-title"`
- Link existing by ID: `instance-name "<name>" --session-id 3`

### 2. Read the Session Doc

```bash
token-ping "session-docs/{doc_id}/content"
```

Read the full content. Orient yourself:
- **## Context** — these are your entry points into the vault
- **## Plan** — this is what you're trying to accomplish
- **## Decisions** — these are choices you (or a previous agent) already made
- **## Activity Log** — this is what's been done so far

### 3. Determine Your Vault

Parse the session doc's `vault` frontmatter field to know which vault you exist in:

| Vault | CLI Name |
|-------|----------|
| Token-ENV | `vault=Token-ENV` |
| Pax-ENV | `vault=Pax-ENV` |
| Claw-ENV | `vault=Claw-ENV` |

### 4. Traverse Wikilinks (Startup Depth: 1 Level)

Extract `[[wikilinks]]` from the `## Context` section. For each link:

```bash
obsidian vault=<name> read path="<resolved_path>.md"
```

**On startup, read only the directly linked notes.** Do not recursively follow links within those notes. The session doc is the curated entry point — if something matters, it should be linked from the session doc.

### 5. Present Context to Yourself

After reading, synthesize your orientation:
- What am I working on? (from Plan)
- What has already been done? (from Activity Log)
- What decisions constrain my work? (from Decisions)
- What domain knowledge do I need? (from resolved vault notes)

Then proceed with the task.

## Runtime Vault Access

During your session, you have full read access to the vault at any time:

```bash
# Search the vault
obsidian vault=<name> search query="<term>"

# Read any note
obsidian vault=<name> read path="<path>.md"

# Find what links to a note
obsidian vault=<name> backlinks path="<path>.md"

# Find orphaned notes
obsidian vault=<name> orphans

# Search with content matching
obsidian vault=<name> search:context query="<term>"
```

Traverse the wiki graph as deep as you need. The startup depth limit is about managing initial context load, not restricting your access.

## No Session Doc?

Not every session needs vault-mind. Ad-hoc sessions, quick tasks, and exploration work without a session doc. The skill exits cleanly if there's nothing to load.

If you're starting significant work and there's no session doc, consider creating one — it's how future agents (and future you) will pick up where you left off.
---
name: vault-mind
description: "Use on session startup when a session document exists. Reads the session doc, traverses wikilinks into the Obsidian vault, and orients the agent with identity-aware context."
---

# Vault Mind — Startup Rite

Read your session document and enter the vault. The Obsidian vault is your extended mind — the session doc is your thread of consciousness.

## When to Use

- On session startup, when you have a linked session document
- When resuming work on a task that has a session doc
- When another agent asks you to pick up a session doc

## Identity Model

**Session doc = self.** You are reading your own memory. "I decided X. I was working on Y. Let me continue."

**Vault notes = institutional.** These were written before you — by other agents, by the Emperor, by the Administratum. Read with fresh judgment. Respect the context, but verify the conclusions.

## Process

### 1. Resolve Your Session Doc

```bash
# Resolve instance ID via PID lookup, then check for linked doc
INSTANCE_ID=$(token-ping "instances" | jq -r --argjson pid "$(ps -o ppid= -p $PPID | tr -d ' ')" '[.[] | select(.pid == $pid)] | .[0].id // empty')
token-ping "instances/$INSTANCE_ID/session-doc"
```

If `session_doc_id` is null, check if you were given a doc ID or path directly. If no session doc exists and the task warrants one:
- Create new: `instance-name "<name>" --session`
- Link existing by title: `instance-name "<name>" --session "existing-doc-title"`
- Link existing by ID: `instance-name "<name>" --session-id 3`

### 2. Read the Session Doc

```bash
token-ping "session-docs/{doc_id}/content"
```

Read the full content. Orient yourself:
- **## Context** — these are your entry points into the vault
- **## Plan** — this is what you're trying to accomplish
- **## Decisions** — these are choices you (or a previous agent) already made
- **## Activity Log** — this is what's been done so far

### 3. Determine Your Vault

Parse the session doc's `vault` frontmatter field to know which vault you exist in:

| Vault | CLI Name |
|-------|----------|
| Token-ENV | `vault=Token-ENV` |
| Pax-ENV | `vault=Pax-ENV` |
| Claw-ENV | `vault=Claw-ENV` |

### 4. Traverse Wikilinks (Startup Depth: 1 Level)

Extract `[[wikilinks]]` from the `## Context` section. For each link:

```bash
obsidian vault=<name> read path="<resolved_path>.md"
```

**On startup, read only the directly linked notes.** Do not recursively follow links within those notes. The session doc is the curated entry point — if something matters, it should be linked from the session doc.

### 5. Present Context to Yourself

After reading, synthesize your orientation:
- What am I working on? (from Plan)
- What has already been done? (from Activity Log)
- What decisions constrain my work? (from Decisions)
- What domain knowledge do I need? (from resolved vault notes)

Then proceed with the task.

## Runtime Vault Access

During your session, you have full read access to the vault at any time:

```bash
# Search the vault
obsidian vault=<name> search query="<term>"

# Read any note
obsidian vault=<name> read path="<path>.md"

# Find what links to a note
obsidian vault=<name> backlinks path="<path>.md"

# Find orphaned notes
obsidian vault=<name> orphans

# Search with content matching
obsidian vault=<name> search:context query="<term>"
```

Traverse the wiki graph as deep as you need. The startup depth limit is about managing initial context load, not restricting your access.

## No Session Doc?

Not every session needs vault-mind. Ad-hoc sessions, quick tasks, and exploration work without a session doc. The skill exits cleanly if there's nothing to load.

If you're starting significant work and there's no session doc, consider creating one — it's how future agents (and future you) will pick up where you left off.
