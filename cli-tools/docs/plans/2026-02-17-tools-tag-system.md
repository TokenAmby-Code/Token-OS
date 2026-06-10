# Tools Tag Browsing System — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the `tool` bash function with a `tools` CLI command that supports tag-based browsing, audience-aware behavior (agent vs human), and directory-scoped tool injection for Claude sessions.

**Architecture:** A single bash script (`bin/tools`) parses inline `# TAGS:` and `# AUDIENCE:` metadata from script headers at runtime. A `directory-tags.yaml` config maps directories to tag sets. The `.bash_aliases` `tool` function is replaced with a `tools` wrapper that passes `--human`.

**Tech Stack:** Bash, grep/awk for header parsing, Claude CLI for AI fallback

---

### Task 1: Add TAGS/AUDIENCE metadata headers to all scripts

**Files:**
- Modify: `bin/pr-create` (line 2 area)
- Modify: `bin/pr-merge` (line 2 area)
- Modify: `bin/pr-review-loop` (line 2 area)
- Modify: `bin/worktree-setup` (line 2 area)
- Modify: `bin/worktree-delete` (line 2 area)
- Modify: `bin/token-restart` (line 2 area)
- Modify: `bin/token-status` (line 2 area)
- Modify: `bin/token-ping` (line 2 area)
- Modify: `bin/tts-skip` (line 2 area)
- Modify: `bin/timer-mode` (line 2 area)
- Modify: `bin/timer-status` (line 2 area)
- Modify: `bin/timer-test` (line 2 area)
- Modify: `bin/deploy` (line 2 area)
- Modify: `bin/cloud-logs` (line 2 area)
- Modify: `bin/db-query` (line 2 area)
- Modify: `bin/db-migrate` (line 2 area)
- Modify: `bin/ssh-phone` (line 2 area)
- Modify: `bin/macrodroid-gen` (line 2 area)
- Modify: `bin/macrodroid-pull` (line 2 area)
- Modify: `bin/macrodroid-push` (line 2 area)
- Modify: `bin/macrodroid-read` (line 2 area)
- Modify: `bin/macrodroid-state` (line 2 area)
- Modify: `bin/tasker-push` (line 2 area)
- Modify: `bin/instance-name` (line 2 area)
- Modify: `bin/instance-stop` (line 2 area)
- Modify: `bin/instances-clear` (line 2 area)
- Modify: `bin/subagent` (line 2 area)
- Modify: `bin/agents-db` (line 2 area)
- Modify: `bin/mem-watchdog` (line 2 area)
- Modify: `bin/time-convert` (line 2 area)
- Modify: `bin/screenshot` (line 2 area)
- Modify: `bin/browser-console` (line 2 area)
- Modify: `bin/sandbox-server` (line 2 area)
- Modify: `bin/stash` (line 2 area)
- Modify: `bin/followup` (line 2 area)
- Modify: `bin/test` (line 2 area)
- Modify: `bin/transplant` (line 2 area)

Insert two lines after the first description comment in each script. The format is:

```bash
# TAGS: <comma-separated tags>
# AUDIENCE: <human|agent|human, agent>
```

Here is the exact metadata for every tool:

```
pr-create:        TAGS: git, pr, workflow          AUDIENCE: human, agent
pr-merge:         TAGS: git, pr                    AUDIENCE: human, agent
pr-review-loop:   TAGS: git, pr                    AUDIENCE: agent
worktree-setup:   TAGS: git, worktree              AUDIENCE: human, agent
worktree-delete:  TAGS: git, worktree              AUDIENCE: human, agent
token-restart:    TAGS: token-api                   AUDIENCE: human, agent
token-status:     TAGS: token-api                   AUDIENCE: human, agent
token-ping:       TAGS: token-api                   AUDIENCE: human, agent
tts-skip:         TAGS: token-api                   AUDIENCE: human
timer-mode:       TAGS: token-api                   AUDIENCE: human, agent
timer-status:     TAGS: token-api                   AUDIENCE: human, agent
timer-test:       TAGS: token-api                   AUDIENCE: agent
deploy:           TAGS: deploy                      AUDIENCE: human, agent
cloud-logs:       TAGS: deploy                      AUDIENCE: human, agent
db-query:         TAGS: db                          AUDIENCE: human, agent
db-migrate:       TAGS: db                          AUDIENCE: human, agent
ssh-phone:        TAGS: mobile, ssh                  AUDIENCE: human
macrodroid-gen:   TAGS: mobile, macrodroid          AUDIENCE: human, agent
macrodroid-pull:  TAGS: mobile, macrodroid          AUDIENCE: human, agent
macrodroid-push:  TAGS: mobile, macrodroid          AUDIENCE: human, agent
macrodroid-read:  TAGS: mobile, macrodroid          AUDIENCE: human
macrodroid-state: TAGS: mobile, macrodroid          AUDIENCE: human, agent
tasker-push:      TAGS: mobile                      AUDIENCE: human, agent
instance-name:    TAGS: instance                    AUDIENCE: agent
instance-stop:    TAGS: instance                    AUDIENCE: agent
instances-clear:  TAGS: instance                    AUDIENCE: agent
subagent:         TAGS: instance                    AUDIENCE: agent
agents-db:        TAGS: instance                    AUDIENCE: agent
mem-watchdog:     TAGS: system                      AUDIENCE: agent
time-convert:     TAGS: system                      AUDIENCE: human
screenshot:       TAGS: system                      AUDIENCE: human, agent
browser-console:  TAGS: system                      AUDIENCE: human, agent
sandbox-server:   TAGS: system                      AUDIENCE: agent
stash:            TAGS: workflow                     AUDIENCE: human
followup:         TAGS: workflow                     AUDIENCE: human
test:             TAGS: workflow                     AUDIENCE: human, agent
transplant:       TAGS: instance                    AUDIENCE: human, agent
```

**Step 1: Add metadata to all git-tagged tools**

For each of pr-create, pr-merge, pr-review-loop, worktree-setup, worktree-delete, find the first description comment line (the line starting with `# ` right after `#!/usr/bin/env bash`) and insert the TAGS/AUDIENCE lines directly after that first description line. Example for pr-create:

Before:
```bash
#!/usr/bin/env bash
# PR Create with Review Polling
#
```

After:
```bash
#!/usr/bin/env bash
# PR Create with Review Polling
# TAGS: git, pr, workflow
# AUDIENCE: human, agent
#
```

Apply the same pattern for all 37 tools using the metadata table above.

**Step 2: Verify metadata was added correctly**

Run:
```bash
grep -l "^# TAGS:" bin/* | wc -l
```
Expected: 37 (all tools have TAGS line)

Run:
```bash
grep -l "^# AUDIENCE:" bin/* | wc -l
```
Expected: 37 (all tools have AUDIENCE line)

**Step 3: Commit**

```bash
git add bin/
git commit -m "feat: add TAGS and AUDIENCE metadata headers to all tools"
```

---

### Task 2: Create the `tools` script — argument parsing and help

**Files:**
- Create: `bin/tools`

**Step 1: Write the script skeleton with arg parsing**

Create `bin/tools` with:

```bash
#!/usr/bin/env bash
# tools - Tag-based CLI tool browser and discovery
# TAGS: system
# AUDIENCE: human, agent
set -euo pipefail

BIN_DIR="$(cd "$(dirname "$0")" && pwd)"
MODE="agent"  # default: non-interactive
FILTER_TAG=""
SHOW_TAGS=false
DIR_FILTER=""

usage() {
    cat <<'USAGE'
tools - Browse and discover CLI tools by tag

Usage:
  tools                        List all tools grouped by primary tag
  tools <tag>                  Filter tools by tag
  tools <query>                Search tool names, AI fallback on miss
  tools --tags                 List all available tags with counts
  tools --agent --dir <path>   Output tools for a directory's tag set
  tools --human                Enable interactive mode (AI + tool creation)
  tools --help                 Show this help

Modes:
  --human    Interactive mode with AI fallback and tool creation prompts
  --agent    Non-interactive mode (default) — safe for automation

Examples:
  tools git                    Show all git-related tools
  tools --tags                 List tags: git(5) deploy(2) db(2) ...
  tools --agent --dir ~/proj   Show tools relevant to ~/proj
USAGE
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --human) MODE="human"; shift ;;
        --agent) MODE="agent"; shift ;;
        --tags)  SHOW_TAGS=true; shift ;;
        --dir)   DIR_FILTER="${2:-}"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *)       FILTER_TAG="$1"; shift ;;
    esac
done
```

**Step 2: Make it executable**

```bash
chmod +x bin/tools
```

**Step 3: Verify help works**

```bash
bin/tools --help
```
Expected: usage text prints without error.

**Step 4: Commit**

```bash
git add bin/tools
git commit -m "feat(tools): script skeleton with arg parsing and help"
```

---

### Task 3: Implement header parsing — scan tools for metadata

**Files:**
- Modify: `bin/tools`

**Step 1: Add the header parsing function**

Add after the arg parsing block:

```bash
# Parse metadata from a script's header comments
# Sets: TOOL_DESC, TOOL_TAGS, TOOL_AUDIENCE
parse_tool_header() {
    local file="$1"
    local name
    name=$(basename "$file")
    TOOL_DESC=""
    TOOL_TAGS=""
    TOOL_AUDIENCE="human, agent"  # default

    # Read first 20 lines for metadata
    local header
    header=$(head -20 "$file" 2>/dev/null || true)

    # Description: first comment line after shebang, extract after " - "
    TOOL_DESC=$(echo "$header" | sed -n '2s/^# .*- //p' | head -1)
    # Fallback: use the whole second line comment
    if [[ -z "$TOOL_DESC" ]]; then
        TOOL_DESC=$(echo "$header" | sed -n '2s/^# //p' | head -1)
    fi

    # TAGS line
    TOOL_TAGS=$(echo "$header" | grep "^# TAGS:" | sed 's/^# TAGS: *//' | head -1)

    # AUDIENCE line
    local aud
    aud=$(echo "$header" | grep "^# AUDIENCE:" | sed 's/^# AUDIENCE: *//' | head -1)
    if [[ -n "$aud" ]]; then
        TOOL_AUDIENCE="$aud"
    fi
}

# Collect all tools into parallel arrays
declare -a ALL_NAMES=()
declare -a ALL_DESCS=()
declare -a ALL_TAGS=()
declare -a ALL_AUDIENCES=()

scan_tools() {
    for file in "$BIN_DIR"/*; do
        [[ -f "$file" ]] || continue
        [[ -x "$file" ]] || continue
        local name
        name=$(basename "$file")
        # Skip self and directories
        [[ "$name" == "tools" ]] && continue
        [[ -d "$file" ]] && continue

        parse_tool_header "$file"
        ALL_NAMES+=("$name")
        ALL_DESCS+=("$TOOL_DESC")
        ALL_TAGS+=("$TOOL_TAGS")
        ALL_AUDIENCES+=("$TOOL_AUDIENCE")
    done
}

scan_tools
```

**Step 2: Add a quick debug test**

```bash
bin/tools --tags
```
Expected: should not error (--tags handler not yet implemented, but scan runs clean). Script exits normally.

**Step 3: Commit**

```bash
git add bin/tools
git commit -m "feat(tools): header parsing to extract TAGS/AUDIENCE/description"
```

---

### Task 4: Implement `--tags` — list all tags with counts

**Files:**
- Modify: `bin/tools`

**Step 1: Add the --tags handler**

Add after `scan_tools` call, before any other display logic:

```bash
# --tags: list all tags with counts
if $SHOW_TAGS; then
    declare -A tag_counts
    for i in "${!ALL_TAGS[@]}"; do
        IFS=', ' read -ra tags <<< "${ALL_TAGS[$i]}"
        for tag in "${tags[@]}"; do
            [[ -z "$tag" ]] && continue
            tag_counts["$tag"]=$(( ${tag_counts["$tag"]:-0} + 1 ))
        done
    done

    # Sort by tag name
    for tag in $(echo "${!tag_counts[@]}" | tr ' ' '\n' | sort); do
        printf "  %-16s %d tools\n" "$tag" "${tag_counts[$tag]}"
    done
    exit 0
fi
```

**Step 2: Verify**

```bash
bin/tools --tags
```
Expected output like:
```
  db               2 tools
  deploy           2 tools
  git              5 tools
  ...
```

**Step 3: Commit**

```bash
git add bin/tools
git commit -m "feat(tools): --tags flag lists all tags with tool counts"
```

---

### Task 5: Implement grouped display — default listing and tag filtering

**Files:**
- Modify: `bin/tools`

**Step 1: Add display functions**

Add after the --tags handler:

```bash
# Check if a tool matches audience filter based on mode
matches_audience() {
    local audience="$1"
    # No audience filtering in default view — show all
    # Audience filtering only applies in --dir mode (handled separately)
    return 0
}

# Check if a tool has a specific tag
has_tag() {
    local tool_tags="$1"
    local target="$2"
    IFS=', ' read -ra tags <<< "$tool_tags"
    for tag in "${tags[@]}"; do
        [[ "$tag" == "$target" ]] && return 0
    done
    return 1
}

# Get the "primary" tag (first tag) for a tool
primary_tag() {
    local tool_tags="$1"
    echo "$tool_tags" | cut -d',' -f1 | xargs
}

# Get "secondary" tags for sub-grouping when filtering
secondary_tags() {
    local tool_tags="$1"
    local filter_tag="$2"
    IFS=', ' read -ra tags <<< "$tool_tags"
    for tag in "${tags[@]}"; do
        [[ "$tag" == "$filter_tag" ]] && continue
        echo "$tag"
        return
    done
    echo "$filter_tag"
}

# Display tools grouped by tag
display_grouped() {
    local filter="${1:-}"

    # Collect matching tools
    declare -a match_idx=()
    for i in "${!ALL_NAMES[@]}"; do
        if [[ -n "$filter" ]]; then
            has_tag "${ALL_TAGS[$i]}" "$filter" || continue
        fi
        match_idx+=("$i")
    done

    if [[ ${#match_idx[@]} -eq 0 ]]; then
        return 1  # no matches
    fi

    # Group by sub-tag (secondary tag when filtering, primary tag otherwise)
    declare -A groups
    declare -a group_order=()
    for i in "${match_idx[@]}"; do
        local group
        if [[ -n "$filter" ]]; then
            group=$(secondary_tags "${ALL_TAGS[$i]}" "$filter")
        else
            group=$(primary_tag "${ALL_TAGS[$i]}")
        fi
        if [[ -z "${groups[$group]+x}" ]]; then
            group_order+=("$group")
        fi
        groups["$group"]+="$i "
    done

    # Display
    for group in "${group_order[@]}"; do
        echo ""
        echo "  ${group}:"
        for i in ${groups[$group]}; do
            printf "    %-20s %s\n" "${ALL_NAMES[$i]}" "${ALL_DESCS[$i]}"
        done
    done
    echo ""
}
```

**Step 2: Add the main display logic**

Add at the end of the script:

```bash
# No filter — show all tools grouped
if [[ -z "$FILTER_TAG" ]] && [[ -z "$DIR_FILTER" ]]; then
    display_grouped
    exit 0
fi

# DIR_FILTER handled in Task 7

# Tag or query filter
if [[ -n "$FILTER_TAG" ]]; then
    # First: try as exact tag match
    if display_grouped "$FILTER_TAG"; then
        exit 0
    fi

    # Second: try as tool name grep
    matches=""
    match_count=0
    for i in "${!ALL_NAMES[@]}"; do
        if echo "${ALL_NAMES[$i]}" | grep -qi "$FILTER_TAG"; then
            matches+="$i "
            ((match_count++))
        fi
    done

    if [[ $match_count -gt 0 ]]; then
        if [[ $match_count -le 2 ]]; then
            # Show --help for 1-2 matches
            for i in $matches; do
                echo "=== ${ALL_NAMES[$i]} ==="
                "$BIN_DIR/${ALL_NAMES[$i]}" --help 2>&1 || echo "(no help available)"
                echo ""
            done
        else
            # List matches with descriptions
            echo ""
            for i in $matches; do
                printf "  %-20s %s\n" "${ALL_NAMES[$i]}" "${ALL_DESCS[$i]}"
            done
            echo ""
        fi
        exit 0
    fi

    # Third: AI fallback
    echo "No tools found matching '$FILTER_TAG'."

    if [[ "$MODE" == "human" ]]; then
        echo "Asking Claude..."
        suggestion=$(cd "$BIN_DIR" && unset CLAUDECODE && claude -p --model haiku \
            "The user searched for '$FILTER_TAG' in these CLI tools: $(ls | tr '\n' ', '). Which tool(s) match what they're looking for? If no existing tool fits, respond with exactly 'NO_MATCH' on its own line and briefly describe what would be needed.")
        echo "$suggestion"

        if echo "$suggestion" | grep -q "NO_MATCH"; then
            echo ""
            read -rp "Do you want to create this tool? Describe it (or 'no' to cancel): " response
            if [[ "$response" != "no" && "$response" != "n" && -n "$response" ]]; then
                (cd "$HOME/Scripts/cli-tools" && unset CLAUDECODE && claude "$response")
            fi
        fi
    else
        # Agent mode: suggestion only, no interactive prompt
        suggestion=$(cd "$BIN_DIR" && unset CLAUDECODE && claude -p --model haiku \
            "The user searched for '$FILTER_TAG' in these CLI tools: $(ls | tr '\n' ', '). Which tool(s) match what they're looking for? If no existing tool fits, respond with exactly 'NO_MATCH' on its own line and briefly describe what would be needed." 2>/dev/null || echo "Could not reach Claude for suggestions.")
        echo "$suggestion"
    fi
fi
```

**Step 3: Verify default listing**

```bash
bin/tools
```
Expected: all tools grouped by primary tag with descriptions.

**Step 4: Verify tag filtering**

```bash
bin/tools git
```
Expected: git tools sub-grouped by pr/worktree.

**Step 5: Verify name search**

```bash
bin/tools macro
```
Expected: macrodroid tools listed with descriptions.

**Step 6: Commit**

```bash
git add bin/tools
git commit -m "feat(tools): grouped display with tag filtering and name search"
```

---

### Task 6: Create directory-tags.yaml config

**Files:**
- Create: `directory-tags.yaml` (in cli-tools root)

**Step 1: Write the config file**

```yaml
# Directory-to-tag mapping for Claude session tool injection
# When a Claude session starts in a directory, tools matching these tags
# are injected as available context.
#
# Use ~ for home directory. Matches are prefix-based (subdirs inherit).
# "all" is a special value that includes every tool.

~/ProcAgentDir:
  tags: [deploy, db, git, pr, worktree, instance, workflow, system]

/Volumes/Imperium/runtimes/token-os/live/token-api:
  tags: [token-api, deploy, instance]

/Volumes/Imperium/runtimes/token-os/live/mobile:
  tags: [mobile, macrodroid]

/Volumes/Imperium/runtimes/token-os/live/cli-tools:
  tags: [all]

~/Scripts:
  tags: [git, workflow, system, instance]
```

**Step 2: Commit**

```bash
git add directory-tags.yaml
git commit -m "feat: directory-tags.yaml maps directories to tool tag sets"
```

---

### Task 7: Implement `--dir` mode — directory-scoped tool injection

**Files:**
- Modify: `bin/tools`

**Step 1: Add the --dir handler**

Add after `scan_tools` call, before the `--tags` handler:

```bash
# --dir: output tools relevant to a directory's tag set
if [[ -n "$DIR_FILTER" ]]; then
    local config_file="$BIN_DIR/../directory-tags.yaml"
    local resolved_dir
    resolved_dir=$(cd "$DIR_FILTER" 2>/dev/null && pwd || echo "$DIR_FILTER")
    local home_expanded
    home_expanded="${resolved_dir/#$HOME/\~}"

    # Find matching directory (longest prefix match)
    local best_match=""
    local best_tags=""

    if [[ -f "$config_file" ]]; then
        local current_dir=""
        local current_tags=""
        while IFS= read -r line; do
            # Skip comments and empty lines
            [[ "$line" =~ ^[[:space:]]*# ]] && continue
            [[ -z "$line" ]] && continue

            # Directory line (not indented, ends with :)
            if [[ "$line" =~ ^[^[:space:]] ]] && [[ "$line" =~ :$ ]]; then
                current_dir="${line%:}"
                current_dir="${current_dir/#\~/$HOME}"
                continue
            fi

            # Tags line
            if [[ "$line" =~ tags:.*\[(.*)\] ]]; then
                current_tags="${BASH_REMATCH[1]}"
                # Check if this dir is a prefix of our target
                if [[ "$resolved_dir" == "$current_dir"* ]]; then
                    if [[ ${#current_dir} -ge ${#best_match} ]]; then
                        best_match="$current_dir"
                        best_tags="$current_tags"
                    fi
                fi
            fi
        done < "$config_file"
    fi

    if [[ -z "$best_tags" ]]; then
        # No match — show workflow + system as baseline
        best_tags="workflow, system, git"
    fi

    # Collect matching tools
    echo "Available CLI tools:"
    IFS=', ' read -ra dir_tags <<< "$best_tags"
    declare -A seen_tools
    for i in "${!ALL_NAMES[@]}"; do
        # Check audience: in agent mode, skip human-only tools
        if [[ "$MODE" == "agent" ]] && [[ "${ALL_AUDIENCES[$i]}" == "human" ]]; then
            continue
        fi

        local dominated=false
        for dt in "${dir_tags[@]}"; do
            dt=$(echo "$dt" | xargs)
            [[ "$dt" == "all" ]] && { dominated=true; break; }
            has_tag "${ALL_TAGS[$i]}" "$dt" && { dominated=true; break; }
        done

        if $dominated && [[ -z "${seen_tools[${ALL_NAMES[$i]}]+x}" ]]; then
            printf "  %-20s - %s\n" "${ALL_NAMES[$i]}" "${ALL_DESCS[$i]}"
            seen_tools["${ALL_NAMES[$i]}"]=1
        fi
    done
    exit 0
fi
```

Note: Since this uses `local` inside the main script body, the `--dir` block should be wrapped in a function. Refactor by wrapping it in `handle_dir_filter()` and calling it, OR remove the `local` keywords and use regular variables. The implementer should choose whichever is cleaner — prefer the function approach.

**Step 2: Verify**

```bash
bin/tools --agent --dir /Volumes/Imperium/runtimes/token-os/live/token-api
```
Expected: tools tagged `token-api`, `deploy`, `instance` — no human-only tools.

```bash
bin/tools --human --dir /Volumes/Imperium/runtimes/token-os/live/cli-tools
```
Expected: all tools (the `all` tag).

**Step 3: Commit**

```bash
git add bin/tools
git commit -m "feat(tools): --dir mode for directory-scoped tool injection"
```

---

### Task 8: Update .bash_aliases — rename `tool` to `tools` wrapper

**Files:**
- Modify: `~/.bash_aliases` (lines 315-364)

**Step 1: Replace the `tool()` function**

Replace the entire `tool()` function block (lines 315-364 of `~/.bash_aliases`) with:

```bash
# 10. TOOL DISCOVERY - Tag-based CLI tool browser
tools() { command tools --human "$@"; }
```

This is a thin wrapper that passes `--human` to enable interactive features (AI fallback + tool creation prompt). Agents calling `tools` directly (without the alias) get agent mode by default.

**Step 2: Verify the alias works**

Source the updated aliases and test:

```bash
source ~/.bash_aliases
type tools
```
Expected: `tools is a function`

**Step 3: Commit**

```bash
git -C ~ add .bash_aliases
git -C ~ commit -m "refactor: replace tool() with tools() wrapper passing --human"
```

---

### Task 9: Create Claude Code session startup hook

**Files:**
- Modify: `~/.claude/hooks.json` (or create if not exists)

**Step 1: Check existing hooks config**

Read `~/.claude/hooks.json` to see existing hook structure.

**Step 2: Add session start hook**

Add a `SessionStart` hook that runs `tools --agent --dir $PWD` and outputs the result as session context. The exact hook format depends on what's already in the hooks file — the implementer should read the current file and add to the existing array.

The hook command should be:

```bash
tools --agent --dir "$PWD"
```

The output should be prefixed with a header like:

```
Available CLI tools for this directory:
```

**Step 3: Verify hook fires**

Start a new Claude Code session in a project directory and confirm the tools list appears in the session context.

**Step 4: Commit**

```bash
git -C ~/.claude add hooks.json
git -C ~/.claude commit -m "feat: session startup hook injects directory-scoped tools list"
```

---

### Task 10: End-to-end verification

**Step 1: Verify all modes**

Run each of these and confirm correct output:

```bash
# Default: all tools grouped
bin/tools

# Tag filter
bin/tools git
bin/tools token-api
bin/tools mobile

# Tag list
bin/tools --tags

# Name search
bin/tools macro
bin/tools timer

# Directory mode
bin/tools --agent --dir ~/ProcAgentDir
bin/tools --agent --dir /Volumes/Imperium/runtimes/token-os/live/token-api
bin/tools --agent --dir /Volumes/Imperium/runtimes/token-os/live/mobile

# Help
bin/tools --help
```

**Step 2: Verify .bash_aliases wrapper**

```bash
source ~/.bash_aliases && tools git
```

**Step 3: Verify metadata coverage**

```bash
# Count tools with TAGS
grep -rl "^# TAGS:" bin/ | wc -l
# Should equal total tool count minus tools itself
ls bin/ | wc -l
```
