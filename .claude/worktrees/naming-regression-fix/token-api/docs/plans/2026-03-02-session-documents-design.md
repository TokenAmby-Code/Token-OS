# Session Documents Design

**Date**: 2026-03-02
**Status**: Approved
**Project**: Token-API

## Summary

Session documents are persistent Obsidian notes that serve as macro plans and activity logs for Claude agent sessions. Each document lives as a markdown file on disk (absolute path stored in DB), can be shared by multiple agents, and is updated by both Claude agents (strategic) and Minimax swarms (volume).

## Goals

1. **Multi-stage coherence** — multiple agents working on the same project share a living document with architectural plan and activity trail
2. **Persistent planning** — session docs outlive individual agent sessions; plan mode becomes scoped to feature specs, not full project scope
3. **Activity tracking** — descriptive log of what each agent did, prescriptive plan they're executing against
4. **Tiered intelligence** — Claude for strategic plan updates, Minimax for high-frequency activity logging and verification

## Data Model

### New Table: `session_documents`

```sql
CREATE TABLE IF NOT EXISTS session_documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT NOT NULL UNIQUE,
    title       TEXT,
    project     TEXT,
    status      TEXT DEFAULT 'active',  -- active | completed | archived
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

### New Column on `claude_instances`

```sql
ALTER TABLE claude_instances ADD COLUMN session_doc_id INTEGER
    REFERENCES session_documents(id) ON DELETE SET NULL
```

Many instances can point to one session doc. `ON DELETE SET NULL` keeps instances functional if a doc is removed.

### Default Sessions Directory

```python
DEFAULT_SESSIONS_DIR = Path.home() / "Token-ENV" / "Sessions"
```

Configurable per-device. The `file_path` column is absolute, so session docs for specific projects can live anywhere (e.g. `~/Scripts/token-api/docs/sessions/`).

## API Endpoints

### Session Document CRUD

```
POST   /api/session-docs                    # Create new session doc
GET    /api/session-docs                    # List all (?status=active&project=token-api)
GET    /api/session-docs/{id}               # Metadata + linked instances
GET    /api/session-docs/{id}/content       # Read file content
PATCH  /api/session-docs/{id}               # Update metadata
DELETE /api/session-docs/{id}               # Archive or hard delete
```

### Instance-Doc Linking

```
POST   /api/instances/{id}/assign-doc       # Assign to existing session doc
POST   /api/instances/{id}/create-doc       # Create new doc + assign
DELETE /api/instances/{id}/unassign-doc     # Unlink (orphan cleanup)
```

### Merge Primitive

```
POST   /api/session-docs/{id}/merge
Body: {
    "content": "raw text or markdown to merge",
    "source": "tui|agent|minimax",
    "context": "optional hint about what this content is"
}
```

## Document Structure

### File Template

```markdown
---
session_doc_id: 3
project: token-api
created: 2026-03-02
agents: []
status: active
---

# Session: <title>

## Plan

_No plan defined yet._

## Activity Log

```

- `agents` frontmatter list updated on assign/unassign
- Plan and Activity Log are conventions, not enforced — agents can add sections freely
- Activity Log entries are reverse-chronological with timestamps and agent attribution

### Activity Log Entry Format

```markdown
### 2026-03-02 14:30 — session-scratchpad-feature
Implemented the session_documents table migration and CRUD endpoints.
Created 4 new endpoints in main.py. Added migration in init_db.py.
```

## The Merge Primitive

Central operation used by all update paths. Takes raw content and intelligently integrates it into the session doc.

### Flow

1. Read current session doc from disk
2. Send doc + new content to LLM with merge instructions
3. LLM decides placement: append to activity log, update plan, add/revise sections
4. Write merged result back to disk
5. Update `updated_at` in DB

### Model Selection

| Source | Model | Rationale |
|--------|-------|-----------|
| `tui` | Minimax | Quick human notes, just needs placement |
| `minimax` | Minimax | Already from Minimax swarm, just merge |
| `agent` | Calling agent's model | Strategic updates need judgment |

### Callers

- **TUI keybind** (`n` for note) → user types text → POST /merge
- **Agent reassignment** → orphan doc content merged into target doc
- **Session update skill** → agent progress summary
- **Minimax hook workers** → verification findings, activity entries

## Instance-Name Integration

The `instance-name` CLI gains session doc support:

```bash
# Create session doc + assign in one step
instance-name "scratchpad-feature" --session

# Create at specific path
instance-name "scratchpad-feature" --session --path ~/Projects/token-api/docs/sessions/

# Assign to existing doc by ID
instance-name "scratchpad-feature" --session-id 3

# Assign to existing doc by title (fuzzy match)
instance-name "scratchpad-feature" --session "scratchpad-feature"
```

## TUI Integration

### Instance Detail Panel

New line showing linked session doc:

```
 Session: scratchpad-feature (~/.../Sessions/2026-03-02-scratchpad-feature.md)
```

### Quick Note Keybind

Press `n` on a selected instance → input prompt appears → user types a sentence or two → Enter → content is merged into the instance's session doc via the merge endpoint.

If the selected instance has no session doc, prompt to create one first.

## Minimax Swarm (Layer 3)

### Hook Triggers

**On `stop` hook** (instance finishes a prompt), if instance has `session_doc_id`:

| Role | Count | Task |
|------|-------|------|
| Activity Scribe | 1 | Summarize what the agent just did → merge into Activity Log |
| Plan Auditor | 1-2 | Read plan + recent activity → flag decisions needing updates |
| Verification Guards | 2-3 | Existing guard lens pattern for code verification |

**On `task_update` to completed:**
- 1 Minimax agent updates session doc with task completion details

### Implementation

Reuses the Minimax API client from `post_run_graph.py`:

```python
_MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
_MINIMAX_MODEL = "MiniMax-M2.5"

async def fire_session_swarm(session_doc_id: int, context: str, roles: list[str]):
    doc = await get_session_doc_content(session_doc_id)
    tasks = [minimax_agent(role, doc, context) for role in roles]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, str):
            await merge_into_doc(session_doc_id, result, source="minimax")
```

### Rate Limiting

Minimax budget: ~300 prompts / 5 hours. Simple sliding window counter:
- Track prompt count per window
- Back off at 80% capacity
- Prioritize verification guards over doc updates when constrained

## Session Update Skill

New skill `session-update` for Claude agents:

1. Read current session doc
2. Gather context: recent task list, git log since last update, current branch state
3. Draft activity log entry + any plan revisions
4. Write back via merge endpoint (or direct file write if running locally)

Invoked:
- Explicitly by agent after significant work
- By stop hook (or delegated to Minimax swarm)
- By brainstorming skill when transitioning to implementation

## Orphan Cleanup

When an instance is reassigned to a different session doc:
1. Check if the old doc has any other instances linked
2. If orphaned (no other instances) AND unedited (content matches template), delete the file and DB row
3. If orphaned but edited, merge its content into the new doc via the merge primitive, then archive the old doc

## Non-Goals (YAGNI)

- No Obsidian plugin — just markdown files Obsidian naturally renders
- No real-time file watching — updates at defined trigger points only
- No complex merge conflict resolution — activity log is append-only, plan managed by one agent at a time
- No auto-creation on registration — explicit opt-in only
- No inline doc rendering in TUI — Obsidian is the viewer

## Implementation Order

1. DB migration (table + column)
2. Session doc CRUD endpoints
3. Instance linking endpoints (assign/create/unassign)
4. Merge primitive (endpoint + Minimax integration)
5. `instance-name --session` CLI integration
6. TUI: session doc path display + quick note keybind
7. Session update skill
8. Minimax swarm hook integration
9. Orphan cleanup logic
