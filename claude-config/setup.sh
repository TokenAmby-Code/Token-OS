#!/bin/bash
# setup.sh — Install unified Claude Code configuration via symlinks to NAS
#
# Creates symlinks from ~/.claude/ and ~/CLAUDE.md to the canonical config
# on the NAS. settings.json is copied (not symlinked) since Claude Code writes to it.
#
# Usage: bash $IMPERIUM/Scripts/claude-config/setup.sh
#
# Safe to re-run — backs up existing files before replacing.

set -euo pipefail

# Resolve config dir (where this script lives)
CONFIG_DIR="$(cd "$(dirname "$0")" && pwd)"

# Verify we're running from the NAS
if [[ ! -f "$CONFIG_DIR/CLAUDE.md" ]]; then
  echo "ERROR: Cannot find $CONFIG_DIR/CLAUDE.md — is the NAS mounted?"
  exit 1
fi

CLAUDE_HOME="$HOME/.claude"
BACKUP_DIR="$CLAUDE_HOME/backup-$(date +%Y%m%d-%H%M%S)"
CHANGES=0

backup_and_remove() {
  local target="$1"
  if [[ -e "$target" || -L "$target" ]]; then
    mkdir -p "$BACKUP_DIR"
    # Preserve the relative path structure in backup
    local basename
    basename=$(basename "$target")
    cp -rL "$target" "$BACKUP_DIR/$basename" 2>/dev/null || true
    rm -rf "$target"
    echo "  Backed up: $target → $BACKUP_DIR/$basename"
  fi
}

link() {
  local src="$1"
  local dst="$2"
  local label="$3"

  # Already correct symlink?
  if [[ -L "$dst" ]] && [[ "$(readlink "$dst")" == "$src" ]]; then
    echo "  OK (already linked): $label"
    return
  fi

  backup_and_remove "$dst"
  ln -s "$src" "$dst"
  echo "  LINKED: $label"
  echo "    $dst → $src"
  CHANGES=$((CHANGES + 1))
}

echo "=== Claude Code Config Setup ==="
echo "Source: $CONFIG_DIR"
echo "Target: $CLAUDE_HOME"
echo ""

# 1. Symlink directories
echo "--- Symlinks ---"
link "$CONFIG_DIR/hooks"    "$CLAUDE_HOME/hooks"    "hooks/"
link "$CONFIG_DIR/commands" "$CLAUDE_HOME/commands"  "commands/"
link "$CONFIG_DIR/skills"   "$CLAUDE_HOME/skills"    "skills/"
link "$CONFIG_DIR/CLAUDE.md" "$HOME/CLAUDE.md"       "~/CLAUDE.md"

# 2. Install settings.json (copy, not symlink — Claude Code writes to it)
echo ""
echo "--- Settings ---"
if [[ -f "$CLAUDE_HOME/settings.json" ]]; then
  # Check if settings matches template (ignoring whitespace)
  CURRENT_HOOKS=$(jq -S '.hooks' "$CLAUDE_HOME/settings.json" 2>/dev/null || echo "null")
  TEMPLATE_HOOKS=$(jq -S '.hooks' "$CONFIG_DIR/settings.template.json" 2>/dev/null || echo "null")

  if [[ "$CURRENT_HOOKS" == "$TEMPLATE_HOOKS" ]]; then
    echo "  OK (hooks match template): settings.json"
  else
    echo "  WARN: settings.json hooks differ from template."
    echo "  Current settings preserved. To reset, run:"
    echo "    cp '$CONFIG_DIR/settings.template.json' '$CLAUDE_HOME/settings.json'"
    echo ""
    echo "  Diff (hook keys only):"
    diff <(echo "$TEMPLATE_HOOKS" | jq 'keys') <(echo "$CURRENT_HOOKS" | jq 'keys') 2>/dev/null || true
  fi
else
  cp "$CONFIG_DIR/settings.template.json" "$CLAUDE_HOME/settings.json"
  echo "  INSTALLED: settings.json (from template)"
  CHANGES=$((CHANGES + 1))
fi

# 3. Summary
echo ""
if [[ $CHANGES -eq 0 ]]; then
  echo "=== No changes needed — config is up to date ==="
else
  echo "=== $CHANGES change(s) applied ==="
  if [[ -d "$BACKUP_DIR" ]]; then
    echo "Backups: $BACKUP_DIR/"
  fi
fi

# Clean up empty backup dir
[[ -d "$BACKUP_DIR" ]] && rmdir "$BACKUP_DIR" 2>/dev/null || true

echo ""
echo "Verify: restart Claude Code and check that /pause, /vault-mind, etc. are available."
