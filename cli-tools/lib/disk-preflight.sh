#!/usr/bin/env bash
# Disk-space preflight helpers for dispatch/worktree mutation paths.

# Default minimum free space for creating/dispatching into worktrees: 2 GiB.
: "${TOKEN_OS_MIN_FREE_KIB:=2097152}"

token_os_free_kib_for_path() {
    local path="$1"
    local probe="$path"
    if [[ -n "${TOKEN_OS_FREE_KIB_OVERRIDE:-}" ]]; then
        printf '%s' "$TOKEN_OS_FREE_KIB_OVERRIDE"
        return 0
    fi
    while [[ ! -e "$probe" && "$probe" != "/" ]]; do
        probe="$(dirname "$probe")"
    done
    df -Pk "$probe" 2>/dev/null | awk 'NR==2 {print $4}'
}

token_os_preflight_free_space() {
    local path="$1" context="${2:-operation}" min_kib="${TOKEN_OS_MIN_FREE_KIB:-2097152}" free_kib
    [[ -n "$path" ]] || path="."
    free_kib="$(token_os_free_kib_for_path "$path")"
    if [[ ! "$min_kib" =~ ^[0-9]+$ || ! "$free_kib" =~ ^[0-9]+$ ]]; then
        echo "disk preflight failed: unable to determine free space for $path" >&2
        return 75
    fi
    if (( free_kib < min_kib )); then
        local free_mib=$(( free_kib / 1024 )) min_mib=$(( min_kib / 1024 ))
        echo "LOW DISK: refusing $context before mutation" >&2
        echo "  target: $path" >&2
        echo "  free: ${free_mib} MiB (${free_kib} KiB)" >&2
        echo "  required: ${min_mib} MiB (${min_kib} KiB)" >&2
        echo "Free disk space on the Data/worktree volume, then retry. No worktree, DB row, pane, or parent process was created by this command." >&2
        return 75
    fi
    return 0
}
