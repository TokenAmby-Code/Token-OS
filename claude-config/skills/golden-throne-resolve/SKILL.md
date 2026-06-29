---
name: golden-throne-resolve
description: Golden Throne resolution step. Use to resolve the current instance, linked session document, and current victory state before acting on an unmet victory condition.
---

# Golden Throne Resolve

Resolve the live instance and read the linked session doc before doing any recovery work.

1. Load machine config if available:
   ```bash
   source "${IMPERIUM:-/Volumes/Imperium}/Imperium-ENV/Scripts/cli-tools/lib/nas-path.sh" 2>/dev/null || true
   ```
2. Resolve the instance:
   ```bash
   CLAUDE_PID=$(pid=$$; for _ in 1 2 3 4 5 6 7 8; do [ -z "$pid" ] || [ "$pid" = "1" ] && break; comm=$(basename "$(ps -o comm= -p "$pid" 2>/dev/null)" 2>/dev/null); [ "$comm" = "claude" ] && echo "$pid" && break; pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' '); done)
   token-ping instances/resolve pid=$CLAUDE_PID cwd="$(pwd)"
   ```
3. Read `session_doc_id` / `session_doc.file_path` from the response.
4. Read current content:
   ```bash
   token-ping "session-docs/<doc_id>/content"
   ```
5. Extract the victory rubric, skip markers, latest activity log, branch/PR state, and explicit blockers.

If there is no linked doc, Golden Throne is misapplied unless the invocation provides one. Escalate or disable the thread via `$golden-throne-close`; do not invent a victory state.
