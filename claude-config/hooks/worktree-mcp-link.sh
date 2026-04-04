#!/usr/bin/env bash
# Auto-symlink .mcp.json into new askCivic worktrees.
# Triggered by WorktreeCreate hook. Reads $CLAUDE_WORKTREE_PATH from stdin JSON.
# Source of truth: /Users/tokenclaw/worktrees/askCivic/.mcp.json

set -euo pipefail

MCP_SOURCE="/Users/tokenclaw/worktrees/askCivic/.mcp.json"

# WorktreeCreate hook receives JSON on stdin with worktree_path
WORKTREE_PATH=$(cat | python3 -c "import sys,json; print(json.load(sys.stdin).get('worktree_path',''))" 2>/dev/null || echo "")

# Only act on askCivic worktrees
if [[ "$WORKTREE_PATH" == /Users/tokenclaw/worktrees/askCivic/* ]] && [[ -f "$MCP_SOURCE" ]]; then
  if [[ ! -e "$WORKTREE_PATH/.mcp.json" ]]; then
    ln -s "$MCP_SOURCE" "$WORKTREE_PATH/.mcp.json"
  fi
fi
