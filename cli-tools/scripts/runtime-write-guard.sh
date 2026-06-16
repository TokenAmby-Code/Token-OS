#!/usr/bin/env bash
# runtime-write-guard.sh — PreToolUse guard that blocks agent writes into the
# deploy-owned runtime checkouts under ~/runtimes (and the NAS mirrors).
#
# WHY: we built the bare-repo machinery so agents stop "dancing on main". The
# local runtime cutover reintroduced the same failure mode in a new shape —
# agents edit ~/runtimes/Token-OS/live (or askCivic) directly instead of in a
# worktree. The runtime checkout is deploy-owned: it advances only via
# `token-restart` (ff-only `git pull`). Direct edits there are drift that the
# next deploy silently clobbers, so we deny them at the tool boundary.
#
# Shared by BOTH harnesses: Claude Code and Codex emit the same PreToolUse wire
# contract — read the tool-call JSON on stdin, deny by printing
#   {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",...}}
# Allow = exit 0 with no stdout. Wire it as a PreToolUse hook in both
# ~/.claude/settings.json and ~/.codex/hooks.json.
#
# Coverage (the "edit tools + precise bash" policy):
#   * Hard deny: Write / Edit / MultiEdit / NotebookEdit and Codex apply_patch
#     whose target file resolves under a runtime root.
#   * Bash/shell: deny only unambiguous in-place mutations aimed at a runtime
#     path (redirection, sed -i, tee, dd of=, truncate, rm, apply_patch/patch
#     bodies, copy/move/link with a runtime destination, direct `git -C <rt>`
#     tree writes). Reads and `token-restart`/deploy verbs pass through.
#
# Escape hatch (deliberate ops): set IMPERIUM_ALLOW_RUNTIME_WRITE=1 in the
# environment, or inline it in the bash command. Use sparingly.
#
# Best-effort & fail-open: any internal error allows the call (never wedge the
# fleet on a guard bug). Everything is logged for tuning.

set -euo pipefail

LOG_DIR="${AGENT_HOOK_LOG_DIR:-${HOME}/.codex/log}"
if [[ "${HOOK_AGENT:-}" == "claude" ]]; then
    LOG_DIR="${AGENT_HOOK_LOG_DIR:-${HOME}/.claude/logs}"
fi
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="${LOG_DIR}/runtime-write-guard.log"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE" 2>/dev/null || true
}

# Fail open: if anything unexpected aborts the script, allow the tool call.
trap 'exit 0' EXIT

HOOK_INPUT="$(cat 2>/dev/null || true)"
[[ -z "$HOOK_INPUT" ]] && HOOK_INPUT="{}"

# Global env escape hatch — deliberate deploy/ops tooling.
if [[ "${IMPERIUM_ALLOW_RUNTIME_WRITE:-}" == "1" ]]; then
    exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
    # No jq: we cannot parse the payload safely. Fail open but record it.
    log "jq not found; allowing (fail-open)"
    exit 0
fi

# ---------------------------------------------------------------------------
# Runtime roots. Match every spelling an agent might emit in a path or command:
# tilde, $HOME/${HOME}, the concrete /Users/<name>, the NAS mirrors, and the
# $TOKEN_OS var (which always resolves under runtimes/Token-OS/live).
# ---------------------------------------------------------------------------
HOME_DIR="${HOME%/}"

# Returns 0 if the given ABSOLUTE, lexically-normalized path is under a runtime root.
abs_under_runtime() {
    local p="$1"
    case "$p" in
        "$HOME_DIR"/runtimes/*|/Volumes/Imperium/runtimes/*|/mnt/imperium/runtimes/*) return 0 ;;
        "$HOME_DIR"/runtimes|/Volumes/Imperium/runtimes|/mnt/imperium/runtimes) return 0 ;;
    esac
    return 1
}

# Lexically resolve a (possibly relative, possibly ~ or $VAR) path to an
# absolute, normalized path WITHOUT requiring it to exist (apply_patch Add File
# targets a not-yet-existing path). No symlink resolution — purely textual.
CWD="$(printf '%s' "$HOOK_INPUT" | jq -r '.cwd // .tool_input.cwd // .env.PWD // empty' 2>/dev/null || true)"
[[ -n "$CWD" ]] || CWD="$PWD"

abspath() {
    local raw="$1" path
    [[ -z "$raw" ]] && return 1

    # Expand the leading env/tilde spellings agents use.
    case "$raw" in
        "~/"*)            raw="${HOME_DIR}/${raw#\~/}" ;;
        "~")              raw="${HOME_DIR}" ;;
        '$HOME/'*)        raw="${HOME_DIR}/${raw#\$HOME/}" ;;
        '${HOME}/'*)      raw="${HOME_DIR}/${raw#\$\{HOME\}/}" ;;
        '$TOKEN_OS/'*)    raw="${TOKEN_OS:-${HOME_DIR}/runtimes/Token-OS/live}/${raw#\$TOKEN_OS/}" ;;
        '${TOKEN_OS}/'*)  raw="${TOKEN_OS:-${HOME_DIR}/runtimes/Token-OS/live}/${raw#\$\{TOKEN_OS\}/}" ;;
    esac

    if [[ "$raw" == /* ]]; then
        path="$raw"
    else
        path="${CWD%/}/$raw"
    fi

    # Lexical normalization of . and .. segments.
    local out=() seg
    local IFS='/'
    for seg in $path; do
        case "$seg" in
            ''|'.') continue ;;
            '..')   [[ ${#out[@]} -gt 0 ]] && unset 'out[${#out[@]}-1]' ;;
            *)      out+=("$seg") ;;
        esac
    done
    printf '/%s' "${out[@]}"
    printf '\n'
}

deny() {
    local target="$1" detail="$2"
    log "DENY tool=${TOOL_NAME:-?} target=${target} (${detail})"
    local reason
    reason="Blocked: write to the deploy-owned runtime checkout ('${target}'). ~/runtimes is advanced only by deploy (token-restart ff-only pull); direct edits are drift the next deploy clobbers. Do this work in a worktree (worktree-setup <branch> -p <project>, lands in ~/worktrees/...) and ship it via PR + token-restart. Deliberate ops only: prefix IMPERIUM_ALLOW_RUNTIME_WRITE=1."
    jq -n --arg r "$reason" '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "deny",
            permissionDecisionReason: $r
        }
    }'
    # The deny verdict is delivered via stdout JSON; exit 0 so the harness reads it.
    trap - EXIT
    exit 0
}

TOOL_NAME="$(
    printf '%s' "$HOOK_INPUT" | jq -r '
        .tool_name // .toolName // .tool // .name //
        .event.tool_name // .event.toolName // .event.tool // .event.name //
        .tool_call.name // .toolCall.name //
        ""
    ' 2>/dev/null || true
)"

# ---------------------------------------------------------------------------
# 1) Explicit edit-tool target paths + any apply_patch body paths anywhere in
#    the payload (covers Codex's dedicated apply_patch tool and edit tools).
# ---------------------------------------------------------------------------
collect_edit_paths() {
    printf '%s' "$HOOK_INPUT" | jq -r '
        def strings:
            if type == "string" then .
            elif type == "array" then .[]? | strings
            elif type == "object" then .[]? | strings
            else empty end;
        [
            .tool_input.file_path?, .tool_input.filepath?, .tool_input.path?,
            .tool_input.notebook_path?,
            .tool_input.files[]?,
            .tool_input.edits[]?.file_path?, .tool_input.edits[]?.path?,
            .input.file_path?, .input.filepath?, .input.path?, .input.notebook_path?,
            .input.files[]?,
            .arguments.file_path?, .arguments.filepath?, .arguments.path?,
            .params.file_path?, .params.filepath?, .params.path?,
            .file_path?, .filepath?, .path?, .notebook_path?
        ] | .[]? | strings
    ' 2>/dev/null

    # apply_patch payloads carry raw patch text; harvest the File: targets.
    printf '%s' "$HOOK_INPUT" | jq -r '.. | strings?' 2>/dev/null | awk '
        /^\*\*\* (Add|Update|Delete) File: / {
            sub(/^\*\*\* (Add|Update|Delete) File: /, "")
            print
        }
    '
}

while IFS= read -r raw_path; do
    [[ -z "$raw_path" ]] && continue
    [[ "$raw_path" == *$'\n'* ]] && continue
    abs="$(abspath "$raw_path" 2>/dev/null || true)"
    [[ -n "$abs" ]] || continue
    if abs_under_runtime "$abs"; then
        deny "$abs" "edit-tool path"
    fi
done < <(collect_edit_paths)

# ---------------------------------------------------------------------------
# 2) Bash / shell command heuristic — precise in-place mutations only.
# ---------------------------------------------------------------------------
CMD="$(
    printf '%s' "$HOOK_INPUT" | jq -r '
        ( .tool_input.command // .tool_input.cmd // .command //
          .input.command // .arguments.command // .params.command // "" ) as $c
        | if ($c | type) == "array" then ($c | join(" ")) else $c end
    ' 2>/dev/null || true
)"

if [[ -n "$CMD" ]]; then
    # Inline escape hatch — only a genuine assignment at the VERY START of the
    # command authorizes. Anchoring to ^ (not any post-separator position) stops
    # a bypass like `echo x > ~/runtimes/...; IMPERIUM_ALLOW_RUNTIME_WRITE=1 true`,
    # where the assignment in a later segment would otherwise green-light the
    # runtime write in an earlier one. A bare `echo IMPERIUM_ALLOW...=1` arg also
    # can't smuggle a bypass, since it doesn't lead with the assignment.
    if printf '%s' "$CMD" | grep -Eq '^[[:space:]]*(env[[:space:]]+)?IMPERIUM_ALLOW_RUNTIME_WRITE=1([[:space:]]|$)'; then
        exit 0
    fi

    # apply_patch / patch heredoc bodies inside a bash command: harvest targets.
    while IFS= read -r raw_path; do
        [[ -z "$raw_path" ]] && continue
        abs="$(abspath "$raw_path" 2>/dev/null || true)"
        [[ -n "$abs" ]] || continue
        if abs_under_runtime "$abs"; then
            deny "$abs" "apply_patch body in bash"
        fi
    done < <(printf '%s' "$CMD" | awk '
        /^\*\*\* (Add|Update|Delete) File: / {
            sub(/^\*\*\* (Add|Update|Delete) File: /, "")
            print
        }')

    # Path-spelling fragment that names a runtime root inside a command. Kept
    # broad on the path side; the mutation context below is what gates a deny.
    # Covers tilde/$HOME, this machine's concrete HOME, any concrete
    # /Users/<name> or /home/<name> home (so a path that doesn't match the
    # hook's own $HOME still trips the guard), the NAS mirrors, and $TOKEN_OS.
    RT_FRAG="(~|\\\$HOME|\\\$\\{HOME\\}|${HOME_DIR}|/Users/[^/[:space:]]+|/home/[^/[:space:]]+|/Volumes/Imperium|/mnt/imperium|\\\$TOKEN_OS|\\\$\\{TOKEN_OS\\})/runtimes/|(\\\$TOKEN_OS|\\\$\\{TOKEN_OS\\})(/|\\b)"

    if printf '%s' "$CMD" | grep -Eq "$RT_FRAG"; then
        # The command references a runtime path. Deny ONLY when paired with an
        # unambiguous in-place mutation. Each pattern requires the runtime path
        # to be the operand/target, not merely present (so reads / copy-out /
        # `token-restart` pass through).

        # a) Output redirection into a runtime path: > , >> , &> , >|
        if printf '%s' "$CMD" | grep -Eq "(>>?|&>|>\\|)[[:space:]]*['\"]?($RT_FRAG)"; then
            deny "(redirection)" "shell redirect into runtime"
        fi

        # b) In-place / destination-target mutators where a runtime path follows
        #    the verb. `sed -i`, `tee`, `dd of=`, `truncate`, `patch`.
        if printf '%s' "$CMD" | grep -Eq "sed[[:space:]]+(-[A-Za-z]*[[:space:]]+)*-i[A-Za-z]*([[:space:]].*)?($RT_FRAG)"; then
            deny "(sed -i)" "sed in-place on runtime"
        fi
        if printf '%s' "$CMD" | grep -Eq "(^|[|;&[:space:]])tee([[:space:]]+-a)?[[:space:]]+['\"]?($RT_FRAG)"; then
            deny "(tee)" "tee into runtime"
        fi
        if printf '%s' "$CMD" | grep -Eq "dd[[:space:]].*of=['\"]?($RT_FRAG)"; then
            deny "(dd of=)" "dd into runtime"
        fi
        if printf '%s' "$CMD" | grep -Eq "(^|[|;&[:space:]])truncate([[:space:]].*)?($RT_FRAG)"; then
            deny "(truncate)" "truncate runtime"
        fi
        if printf '%s' "$CMD" | grep -Eq "(^|[|;&[:space:]])patch([[:space:]].*)?($RT_FRAG)"; then
            deny "(patch)" "patch into runtime"
        fi

        # c) rm / mkdir / touch / chmod / chown / ln naming a runtime path.
        if printf '%s' "$CMD" | grep -Eq "(^|[|;&[:space:]])(rm|mkdir|touch|chmod|chown|chgrp|ln)([[:space:]]+-[^[:space:]]+)*[[:space:]].*($RT_FRAG)"; then
            deny "(rm/mkdir/touch/chmod/ln)" "fs mutation on runtime"
        fi

        # d) cp / mv / rsync / install / mvn-like: flag when a runtime path is
        #    the DESTINATION (last operand of a command segment). Splitting on
        #    shell separators, a segment whose final token is a runtime path and
        #    whose verb is a copier is a write INTO runtime (copy-out is allowed).
        if printf '%s' "$CMD" | awk -v RS='[;&|]+' '
                {
                    seg=$0
                    if (seg ~ /(^|[ \t])(cp|mv|rsync|install|scp)([ \t]|$)/) {
                        n=split(seg, t, /[ \t]+/)
                        last=t[n]
                        gsub(/^['\''"]+|['\''"]+$/, "", last)
                        if (last ~ /(\/runtimes\/|\$TOKEN_OS|\$\{TOKEN_OS\})/) { found=1 }
                    }
                }
                END { exit(found?0:1) }
            '; then
            deny "(cp/mv/rsync dest)" "copy/move into runtime"
        fi

        # e) Direct git tree writes into a runtime checkout via -C / --git-dir.
        #    Allow arbitrary intervening options BOTH before the dir flag (e.g.
        #    `git -c core.fsmonitor=false -C <rt> reset`) and between the runtime
        #    path and the write subcommand (e.g. `git -C <rt> -c advice...=false
        #    reset --hard`), all within the same command segment.
        if printf '%s' "$CMD" | grep -Eq "git[[:space:]][^;&|]*(-C|--git-dir=?|--work-tree=?)[[:space:]=]*['\"]?($RT_FRAG)[^;&|]*[[:space:]](reset|checkout|clean|restore|apply|stash|merge|rebase|cherry-pick|commit|add|rm|mv|switch)([[:space:]]|$)"; then
            deny "(git tree write)" "direct git write into runtime"
        fi
    fi
fi

# Nothing matched — allow.
exit 0
