---
name: session-update
description: "Use when completing significant work, making architectural decisions, or before ending a session that has a linked session document"
---

# Session Update

Update this session's persistent session document with progress, decisions, and plan revisions.

## When to Use

- After completing a significant task or milestone
- When architectural decisions are made that affect the plan
- When the plan needs revision based on new findings
- Before ending a session (summarize what was accomplished)

## Process

1. **Resolve your identity** (one call — returns instance + session doc):
   ```bash
   # Walk up to find the claude PID, then resolve via API
   CLAUDE_PID=$(pid=$$; for _ in 1 2 3 4 5; do [ -z "$pid" ] || [ "$pid" = "1" ] && break; comm=$(basename "$(ps -o comm= -p "$pid" 2>/dev/null)" 2>/dev/null); [ "$comm" = "claude" ] && echo "$pid" && break; pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' '); done)
   token-ping instances/resolve pid=$CLAUDE_PID cwd=$(pwd)
   ```
   Response includes `id`, `session_doc_id`, and `session_doc` (with `id`, `title`, `file_path`, `status`).

   If `session_doc` is null — no linked doc. Create or link one:
   - Create new: `instance-name "<name>" --session`
   - Link existing by title: `instance-name "<name>" --session "existing-doc-title"`
   - Link existing by ID: `instance-name "<name>" --session-id 3`

2. **Read current doc:**
   ```bash
   token-ping "session-docs/{doc_id}/content"
   ```

3. **Gather context** — current task list, recent `git log --oneline -10`, key decisions or blockers.

4. **Merge via API:**
   ```bash
   curl -s -X POST "http://localhost:7777/api/session-docs/{doc_id}/merge" \
     -H "Content-Type: application/json" \
     -d '{"content": "your update text", "source": "agent", "context": "Progress update after completing X"}'
   ```

## Self-Reassignment

Every top-level session auto-creates a session doc on SessionStart. If you need to switch to a different doc (e.g., you're picking up prior work that has its own doc), you can reassign yourself:

```bash
# Resolve your own instance
CLAUDE_PID=$(pid=$$; for _ in 1 2 3 4 5; do [ -z "$pid" ] || [ "$pid" = "1" ] && break; comm=$(basename "$(ps -o comm= -p "$pid" 2>/dev/null)" 2>/dev/null); [ "$comm" = "claude" ] && echo "$pid" && break; pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' '); done)
token-ping instances/resolve pid=$CLAUDE_PID cwd=$(pwd)

# Assign to an existing doc by ID
token-ping instances/<id>/assign-doc doc_id=<N>

# Or create + assign a new one
token-ping instances/<id>/create-doc title="my session"
```

When you reassign, the auto-created doc from SessionStart is left behind (orphan cleanup will handle it if unused).

## Activity Log Entry Format

```markdown
### 2026-03-02 14:30 -- instance-name
Implemented X, Y, Z. Modified files: a.py, b.py.
Decision: chose approach A over B because [reason].
```

## Completing a Session

When a session is ending and all work is done:

1. **Final activity log merge** — merge a summary of what was accomplished
2. **Mark completed:**
   ```bash
   curl -s -X PATCH "http://localhost:7777/api/session-docs/{doc_id}" \
     -H "Content-Type: application/json" \
     -d '{"status": "completed"}'
   ```
3. **Deploy** (sends to Administratum queue):
   ```bash
   curl -s -X POST "http://localhost:7777/api/session-docs/{doc_id}/deploy"
   ```

## Quick Reference

| Action | Endpoint |
|--------|----------|
| Resolve self | `GET /api/instances/resolve?pid=X&cwd=Y` |
| Read doc content | `GET /api/session-docs/{id}/content` |
| Merge update | `POST /api/session-docs/{id}/merge` |
| Mark completed | `PATCH /api/session-docs/{id}` with `{"status": "completed"}` |
| Deploy to queue | `POST /api/session-docs/{id}/deploy` |
| Reassign doc | `POST /api/instances/{id}/assign-doc` with `doc_id=N` |
| Create + assign | `POST /api/instances/{id}/create-doc` with `title=X` |
