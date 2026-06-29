---
name: session-update
description: Mandatory continuity procedure for updating a linked Token-OS session document. Use when completing significant work, making decisions, changing plans, hitting blockers, before ending a session, or when a persona/rank doc says session continuity is required.
---

# Session Update

Update the linked session document with progress, decisions, validation, blockers, and next steps. This is continuity infrastructure, not optional journaling.

## Procedure

1. Resolve this instance and session doc:
   ```bash
   source "${IMPERIUM:-/Volumes/Imperium}/Imperium-ENV/Scripts/cli-tools/lib/nas-path.sh" 2>/dev/null || true
   CLAUDE_PID=$(pid=$$; for _ in 1 2 3 4 5 6 7 8; do [ -z "$pid" ] || [ "$pid" = "1" ] && break; comm=$(basename "$(ps -o comm= -p "$pid" 2>/dev/null)" 2>/dev/null); [ "$comm" = "claude" ] && echo "$pid" && break; pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' '); done)
   token-ping instances/resolve pid=$CLAUDE_PID cwd="$(pwd)"
   ```
   The response should include `id`, `session_doc_id`, and `session_doc`.
2. If no doc is linked, create or assign one intentionally:
   ```bash
   instance-name "descriptive-kebab-title" --session
   token-ping instances/<instance_id>/assign-doc doc_id=<doc_id>
   token-ping instances/<instance_id>/create-doc title="descriptive title"
   ```
3. Read the current doc before merging:
   ```bash
   token-ping "session-docs/<doc_id>/content"
   ```
4. Gather facts: files changed, commits/PRs/SHAs, tests, live verification, decisions, blockers, and remaining gates.
5. Merge a concise update:
   ```bash
   curl -s -X POST "$TOKEN_API_URL/api/session-docs/<doc_id>/merge" \
     -H "Content-Type: application/json" \
     -d @/tmp/session-update.json
   ```
   JSON shape:
   ```json
   {"content":"### YYYY-MM-DD HH:MM -- instance-name\nUpdate text...","source":"agent","context":"Progress update after <work>"}
   ```

## Entry Shape

```markdown
### YYYY-MM-DD HH:MM -- instance-name
Implemented/decided/validated X. Modified files: a, b.
Validation: command/output summary.
Blockers/gates: any remaining approval, merge, deploy, live verification, or external dependency.
Next: concrete next step, or "complete".
```

## Completion and Deploy

When all work is complete:

1. Merge the final activity summary.
2. Mark the doc completed:
   ```bash
   curl -s -X PATCH "$TOKEN_API_URL/api/session-docs/<doc_id>" \
     -H "Content-Type: application/json" \
     -d '{"status":"completed"}'
   ```
3. Deploy it to the Administratum queue:
   ```bash
   curl -s -X POST "$TOKEN_API_URL/api/session-docs/<doc_id>/deploy"
   ```

Do not mark a doc completed if merge/deploy/live verification or an assigned victory condition remains open.
