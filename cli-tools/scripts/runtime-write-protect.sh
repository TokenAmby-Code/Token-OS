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

# Runtime-writable directories: subpaths the RUNNING app must open for write at
# runtime even though the rest of the deploy-owned tree is frozen read-only.
# These are exempted from the lock (re-granted owner-write, created if missing)
# and ignored by the write-bit verification, so locking the tree no longer
# clobbers the live queue. Paths are relative to each runtime root; override the
# default with TOKEN_OS_RUNTIME_WRITABLE_DIRS (':' or space separated).
#
# Audited write sites under the runtime tree (paths relative to the root):
#   pending/  -> discord-daemon job queue (discord-daemon/message-store.js;
#                token-api /send enqueues here). THE incident path — a missing
#                owner-write bit on this dir fired EACCES fleet-wide.
# token-api/restart_state.json was the only other runtime write into the tree;
# it was relocated to ~/.claude/ (out of the deploy tree) rather than exempted,
# because a lone mutable file inside a read-only source dir can't satisfy the
# pragma-once unlink (delete needs a writable parent dir, which stays frozen).
writable_rel_dirs() {
    local raw="${TOKEN_OS_RUNTIME_WRITABLE_DIRS-pending}"
    local d
    local IFS=': '
    for d in $raw; do
        [[ -n "$d" ]] && printf '%s\n' "$d"
    done
}

default_roots() {
    # Local-filesystem runtimes only. chmod is a silent no-op on network mounts
    # (SMB/CIFS — e.g. /Volumes/Imperium, /mnt/imperium), so the boundary cannot
    # enforce there; never list a path we can't actually lock. NAS runtimes are
    # retired by the local-runtime cutover, not protected here.
    local roots=()
    roots+=("${TOKEN_OS_RUNTIME_CHECKOUT:-${HOME_DIR}/runtimes/Token-OS/live}")
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
    # Portable read loop instead of `mapfile` (bash 4+) so the no-arg default
    # path works on macOS's stock bash 3.2 too.
    ROOTS=()
    while IFS= read -r _root; do
        ROOTS+=("$_root")
    done < <(default_roots)
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

# Create any missing runtime-writable exemption BEFORE the freeze, while the
# tree is still writable (deploy unlocks before it locks). Creation matters for
# a fresh deploy: once the root is read-only the app can't mkdir the queue
# itself, so the lock must leave the exemption present. On a re-lock of an
# already-frozen tree, briefly restore owner write on the root so mkdir lands;
# the freeze that follows resets it.
ensure_writable_exemptions_exist() {
    local root="$1" rel target
    while IFS= read -r rel; do
        [[ -n "$rel" ]] || continue
        target="$root/$rel"
        [[ -L "$target" ]] && continue   # symlinks reported in the re-grant pass
        if [[ ! -e "$target" ]]; then
            chmod u+w "$root" 2>/dev/null || true
            mkdir -p "$target"
        fi
    done < <(writable_rel_dirs)
}

# Re-grant owner write to each exemption AFTER the freeze clobbered it. group/
# other stay write-less and symlink targets are never chmod'd. A symlink where a
# writable dir is expected is refused (rc=1) — never silently widen a link.
regrant_writable_exemptions() {
    local root="$1" rel target
    while IFS= read -r rel; do
        [[ -n "$rel" ]] || continue
        target="$root/$rel"
        if [[ -L "$target" ]]; then
            echo "runtime-write-protect: writable exemption is a symlink, refusing: $target" >&2
            rc=1
            continue
        fi
        [[ -e "$target" ]] || continue
        chmod_tree_no_symlinks "$target" u+w
    done < <(writable_rel_dirs)
}

# Build a find prune expression that skips the writable exemptions under $root,
# so the write-bit verification ignores the dirs we deliberately keep writable.
exemption_prune_expr() {
    local root="$1" rel
    EXEMPTION_PRUNE=()
    while IFS= read -r rel; do
        [[ -n "$rel" ]] || continue
        EXEMPTION_PRUNE+=( -path "$root/$rel" -o -path "$root/$rel/*" -o )
    done < <(writable_rel_dirs)
    # Drop the trailing -o so the group is a well-formed expression.
    if [[ ${#EXEMPTION_PRUNE[@]} -gt 0 ]]; then
        unset 'EXEMPTION_PRUNE[$((${#EXEMPTION_PRUNE[@]}-1))]'
    fi
}

# True if any non-exempt path under $root still carries a write bit. The
# exemptions are pruned so a correctly-locked tree (frozen source + writable
# queue) reads as locked, not as a false "still writable".
root_has_write_bits() {
    local root="$1"
    exemption_prune_expr "$root"
    if [[ ${#EXEMPTION_PRUNE[@]} -gt 0 ]]; then
        find -P "$root" \( "${EXEMPTION_PRUNE[@]}" \) -prune -o \
            ! -type l \( -perm -u+w -o -perm -g+w -o -perm -o+w \) -print -quit | grep -q .
    else
        find -P "$root" \
            ! -type l \( -perm -u+w -o -perm -g+w -o -perm -o+w \) -print -quit | grep -q .
    fi
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
            # Create exemptions while writable, freeze the whole tree, then
            # re-grant write to the exemptions. Net result: frozen source, 0755
            # runtime queue. Idempotent — a re-deploy onto a correct tree leaves
            # the exemptions 0755.
            ensure_writable_exemptions_exist "$root"
            chmod_tree_no_symlinks "$root" u-w,go-w
            regrant_writable_exemptions "$root"
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
