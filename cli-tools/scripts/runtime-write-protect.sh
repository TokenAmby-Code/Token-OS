#!/usr/bin/env bash
# runtime-write-protect.sh — chmod boundary for deploy-owned runtime checkouts.
#
# This is intentionally filesystem-level, not just a prompt/hook hint. Runtime
# checkouts should be readable/executable for agents but not writable; deploy
# code unlocks them only for the narrow git update window and locks them again.

set -euo pipefail

usage() {
    cat <<'USAGE'
runtime-write-protect.sh lock|unlock|status|assert-locked [runtime-root ...]

Actions:
  lock           remove write bits recursively (symlinks are not followed)
  unlock         restore owner write bits recursively for deploy
  status         print locked/unlocked/missing per root; exits 0
  assert-locked  exits nonzero if any existing root has a write bit

If no roots are supplied, protects known Token-OS runtime checkouts on this host.
USAGE
}

ACTION="${1:-}"
if [[ -z "$ACTION" || "$ACTION" == "-h" || "$ACTION" == "--help" ]]; then
    usage
    exit 0
fi
shift || true

case "$ACTION" in
    lock|unlock|status|assert-locked) ;;
    *)
        echo "runtime-write-protect: unknown action: $ACTION" >&2
        usage >&2
        exit 2
        ;;
esac

HOME_DIR="${HOME%/}"

default_roots() {
    local roots=()
    roots+=("${TOKEN_OS_RUNTIME_CHECKOUT:-${HOME_DIR}/runtimes/Token-OS/live}")
    roots+=("/Volumes/Imperium/runtimes/token-os/live")
    roots+=("/mnt/imperium/runtimes/token-os/live")
    roots+=("/home/token/runtimes/token-os/live")

    local seen=""
    local root
    for root in "${roots[@]}"; do
        [[ -n "$root" ]] || continue
        case ":$seen:" in *":$root:"*) continue ;; esac
        seen="$seen:$root"
        printf '%s\n' "$root"
    done
}

if [[ $# -gt 0 ]]; then
    ROOTS=("$@")
else
    mapfile -t ROOTS < <(default_roots)
fi

is_git_checkout() {
    [[ -d "$1/.git" || -f "$1/.git" ]]
}

# Mode flips are operational policy, not source changes. On filesystems that
# report executable/write mode changes, leave git status clean after locking.
disable_git_filemode_tracking() {
    local root="$1"
    if is_git_checkout "$root"; then
        git -C "$root" config core.filemode false 2>/dev/null || true
    fi
}

chmod_tree_no_symlinks() {
    local root="$1" mode="$2"
    # find -P is the default, but spell it out: do not chmod symlink targets
    # such as secrets mounted into the runtime checkout.
    find -P "$root" ! -type l -exec chmod "$mode" {} +
}

root_has_write_bits() {
    local root="$1"
    find -P "$root" ! -type l \( -perm -u+w -o -perm -g+w -o -perm -o+w \) -print -quit | grep -q .
}

rc=0
for root in "${ROOTS[@]}"; do
    if [[ ! -e "$root" ]]; then
        [[ "$ACTION" == "status" ]] && echo "missing $root"
        continue
    fi
    if [[ -L "$root" ]]; then
        echo "runtime-write-protect: runtime root must not be a symlink: $root" >&2
        rc=1
        continue
    fi
    if [[ ! -d "$root" ]]; then
        echo "runtime-write-protect: not a directory: $root" >&2
        rc=1
        continue
    fi

    case "$ACTION" in
        unlock)
            chmod_tree_no_symlinks "$root" u+w
            disable_git_filemode_tracking "$root"
            echo "unlocked $root"
            ;;
        lock)
            disable_git_filemode_tracking "$root"
            chmod_tree_no_symlinks "$root" u-w,go-w
            # Verify the lock actually took. Network mounts (SMB/CIFS) silently
            # ignore POSIX mode changes, so chmod "succeeds" while the tree stays
            # writable. Never report a lock we didn't achieve — that is false
            # security, and deploy callers gate on this exit status.
            if root_has_write_bits "$root"; then
                echo "runtime-write-protect: lock did NOT take on $root: write bits remain after chmod (likely a network mount that ignores POSIX modes). NOT write-protected." >&2
                rc=1
            else
                echo "locked $root"
            fi
            ;;
        status)
            if root_has_write_bits "$root"; then
                echo "unlocked $root"
            else
                echo "locked $root"
            fi
            ;;
        assert-locked)
            if root_has_write_bits "$root"; then
                echo "unlocked $root" >&2
                rc=1
            fi
            ;;
    esac
done

exit "$rc"
