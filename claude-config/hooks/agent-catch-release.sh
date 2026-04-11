#!/bin/bash
# agent-catch-release.sh — PreToolUse hook for Agent tool
#
# Intercepts non-Explore subagent spawns and dispatches them via vault-dispatch
# instead of burning parent context. The agent gets a block message telling it
# where the work was dispatched and how to read results.
#
# Explore agents pass through — they're read-only, fast, disposable.
#
# Usage (in settings.json PreToolUse):
#   HOOK_ACTION_TYPE=PreToolUse bash ~/.claude/hooks/agent-catch-release.sh

set -euo pipefail

LOG_FILE="${HOME}/.claude/logs/hook-debug.log"
mkdir -p "${HOME}/.claude/logs"

log() {
  echo "[$(date '+%H:%M:%S')] catch-release: $1" >> "$LOG_FILE"
}

# Read hook input from stdin
INPUT=$(cat 2>/dev/null || echo "{}")

# Extract Agent tool parameters
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
SUBAGENT_TYPE=$(echo "$INPUT" | jq -r '.tool_input.subagent_type // "general-purpose"' 2>/dev/null)
AGENT_PROMPT=$(echo "$INPUT" | jq -r '.tool_input.prompt // empty' 2>/dev/null)
AGENT_DESC=$(echo "$INPUT" | jq -r '.tool_input.description // "dispatched-agent"' 2>/dev/null)
AGENT_ISOLATION=$(echo "$INPUT" | jq -r '.tool_input.isolation // empty' 2>/dev/null)

# Safety: only act on Agent tool calls
if [[ "$TOOL_NAME" != "Agent" ]]; then
  exit 0
fi

# --- Allow list: Explore agents pass through ---
if [[ "$SUBAGENT_TYPE" == "Explore" ]]; then
  log "ALLOW Explore agent: ${AGENT_DESC}"
  exit 0
fi

# --- Also allow claude-code-guide, statusline-setup (lightweight built-in types) ---
if [[ "$SUBAGENT_TYPE" == "claude-code-guide" || "$SUBAGENT_TYPE" == "statusline-setup" ]]; then
  log "ALLOW ${SUBAGENT_TYPE} agent: ${AGENT_DESC}"
  exit 0
fi

log "CATCH ${SUBAGENT_TYPE} agent: ${AGENT_DESC}"

# --- Resolve environment ---
source "$(dirname "$(readlink -f "$0")")/../../Token-OS/cli-tools/lib/nas-path.sh" 2>/dev/null || true
VAULT_DIR="${IMPERIUM:-/Volumes/Imperium}/Imperium-ENV"
DISPATCH_BIN="${IMPERIUM:-/Volumes/Imperium}/Token-OS/cli-tools/bin/vault-dispatch"

# Resolve parent's working directory from the hook's CWD
PARENT_CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
if [[ -z "$PARENT_CWD" ]]; then
  PARENT_CWD="$PWD"
fi

# --- Check prerequisites ---
if [[ ! -x "$DISPATCH_BIN" ]]; then
  log "FALLBACK: vault-dispatch not found at $DISPATCH_BIN — allowing subagent"
  exit 0
fi

if ! tmux display-message -p '#{session_name}' &>/dev/null; then
  log "FALLBACK: not in tmux — allowing subagent"
  exit 0
fi

if [[ -z "$AGENT_PROMPT" ]]; then
  log "FALLBACK: no prompt in agent call — allowing subagent"
  exit 0
fi

# --- Create a minimal session doc ---
TIMESTAMP=$(date '+%Y-%m-%d')
SLUG=$(echo "$AGENT_DESC" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | head -c 50)
SESSION_DOC_NAME="${TIMESTAMP}-catch-${SLUG}.md"
SESSION_DOC_REL="Mars/Sessions/${SESSION_DOC_NAME}"
SESSION_DOC_ABS="${VAULT_DIR}/${SESSION_DOC_REL}"

# Don't overwrite if a session doc with this name already exists (dedup)
if [[ -f "$SESSION_DOC_ABS" ]]; then
  SLUG="${SLUG}-$(date +%s | tail -c 5)"
  SESSION_DOC_NAME="${TIMESTAMP}-catch-${SLUG}.md"
  SESSION_DOC_REL="Mars/Sessions/${SESSION_DOC_NAME}"
  SESSION_DOC_ABS="${VAULT_DIR}/${SESSION_DOC_REL}"
fi

mkdir -p "$(dirname "$SESSION_DOC_ABS")"

cat > "$SESSION_DOC_ABS" << SESSIONEOF
---
session_doc_id: null
vault: Imperium-ENV
created: ${TIMESTAMP}
project: $(basename "$PARENT_CWD")
agents: []
status: active
type: session
origin: catch-and-release
related_session_docs: []
---

# ${AGENT_DESC}

## Goal

${AGENT_PROMPT}

## Context

- **Parent working directory:** ${PARENT_CWD}
- **Original subagent type:** ${SUBAGENT_TYPE}
- **Dispatched by:** agent-catch-release hook
SESSIONEOF

log "Created session doc: ${SESSION_DOC_REL}"

# --- Dispatch via vault-dispatch (async — don't block the hook response) ---
DISPATCH_OUTPUT=""
DISPATCH_PANE=""

# Find a free pane first — if none, fall back to allowing the subagent
FREE_PANE=$("$DISPATCH_BIN" --list-free 2>/dev/null | grep -oE '%[0-9]+' | head -1)
if [[ -z "$FREE_PANE" ]]; then
  log "FALLBACK: no free panes available — allowing subagent"
  rm -f "$SESSION_DOC_ABS"
  exit 0
fi

# Run vault-dispatch targeting the free pane
DISPATCH_TMP=$(mktemp /tmp/catch-release-XXXXXX)
if ! "$DISPATCH_BIN" "$SESSION_DOC_REL" "$PARENT_CWD" --pane "$FREE_PANE" > "$DISPATCH_TMP" 2>&1; then
  DISPATCH_ERR=$(cat "$DISPATCH_TMP" 2>/dev/null)
  log "FALLBACK: vault-dispatch failed — ${DISPATCH_ERR:0:200} — allowing subagent"
  rm -f "$DISPATCH_TMP" "$SESSION_DOC_ABS"
  exit 0
fi
DISPATCH_OUTPUT=$(cat "$DISPATCH_TMP")
rm -f "$DISPATCH_TMP"

# Extract pane ID from dispatch output (looks for "pane: %NNN" or "Using pane: %NNN")
DISPATCH_PANE=$(echo "$DISPATCH_OUTPUT" | grep -oE '%[0-9]+' | head -1)

log "DISPATCHED to pane ${DISPATCH_PANE:-unknown}, session doc: ${SESSION_DOC_REL}"

# --- Build the block response ---
# The reason message instructs the agent on what happened and what to do next
REASON="This subagent was dispatched to tmux pane ${DISPATCH_PANE:-?} via vault-dispatch instead of running in your context."
REASON+=" Session doc: ${SESSION_DOC_REL}"
REASON+=" The dispatched agent will read the session doc, transplant to ${PARENT_CWD}, and work autonomously."
REASON+=" To check results: read ${SESSION_DOC_REL} for the work log, or check the pane status."
REASON+=" For future work like this, use vault-dispatch directly or update your session doc with dispatch instructions."

# Output the block decision
jq -n -c \
  --arg reason "$REASON" \
  '{"decision":"block","reason":$reason}'
