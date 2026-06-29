#!/usr/bin/env bash
# runtime-write-protect.sh — chmod boundary for deploy-owned runtime checkouts.
#
# This is intentionally filesystem-level, not just a prompt/hook hint. Runtime
# checkouts should be readable/executable for agents but not writable; deploy
# code unlocks them only for the narrow git update window and locks them again.
#
# Two layers, weakest first:
#   1. chmod u-w,go-w  — clears write bits. Owner-bypassable: an agent runs as
#      the same uid that owns the tree, and an owner can always `chmod u+w` its
#      own files. This is a speed bump, not a wall.
#   2. chflags uchg    — sets the BSD user-immutable flag (macOS only). With it,
#      `chmod u+w` itself returns EPERM, so the demonstrated bypass (chmod then
#      write) fails at the OS layer; an attacker must additionally know to run
#      `chflags nouchg`. Still owner-clearable, so it is defense-in-depth, not a
#      privilege boundary — true agent-proofing needs a different uid/root, which
#      this host's deploy (same uid, no passwordless sudo) cannot provide.
# The immutable layer is capability-gated: skipped where chflags is absent
# (Linux satellites), which fall back to chmod-only as before.

set -euo pipefail

usage() {
    cat <<'USAGE'
runtime-write-protect.sh lock|unlock [--force|--admin-force] [runtime-root ...]
runtime-write-protect.sh status|assert-locked [runtime-root ...]

Actions:
  lock           clear write bits + set the user-immutable flag (macOS) recursively
  unlock         clear the immutable flag + restore owner write bits for deploy
                 --force/--admin-force is an explicit sanctioned-admin marker
  status         print locked/unlocked/missing per root; exits 0
  assert-locked  exits nonzero if any root has a write bit or is missing uchg

If no roots are supplied, protects known Token-OS runtime checkouts on this host.
Use unlock --force only for deliberate sanctioned admin/runtime maintenance;
never use it to make a quick code change in the deploy-owned runtime.
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

ADMIN_FORCE=0
if [[ "$ACTION" == "unlock" ]]; then
    FILTERED_ARGS=()
    for arg in "$@"; do
        case "$arg" in
            --force|--admin-force)
                ADMIN_FORCE=1
                ;;
            --)
                ;;
            *)
                FILTERED_ARGS+=("$arg")
                ;;
        esac
    done
    set -- "${FILTERED_ARGS[@]}"
fi

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

# Transient git lock files (HEAD.lock, index.lock, refs/**/*.lock, gc.log.lock)
# are created and deleted by git mid-operation. Freezing one (uchg) would break
# git; worse, a lock that vanishes between `find` listing it and the batched
# `-exec` running yields `chmod/chflags: …/X.lock: No such file or directory` — a
# nonzero exit that falsely fails the whole lock (the concurrent-deploy race).
# Prune anything under a `.git/` dir ending in `.lock` from every tree pass.
# `-path '*/.git/*.lock'` matches at any depth (find's -path `*` spans '/'), and
# is scoped to `.git` so a tracked lockfile like uv.lock (token-api/uv.lock) stays
# frozen. Used as the leading clause of an `-prune -o <rest>` expression.
GIT_LOCK_PRUNE=( -path '*/.git/*.lock' -prune -o )

chmod_tree_no_symlinks() {
    local root="$1" mode="$2"
    # find -P is the default, but spell it out: do not chmod symlink targets
    # such as secrets mounted into the runtime checkout.
    find -P "$root" "${GIT_LOCK_PRUNE[@]}" ! -type l -exec chmod "$mode" {} +
}

# The user-immutable layer (chflags uchg) is BSD/macOS only. Linux satellites
# have no chflags (and `chattr +i` would need root), so they fall back to the
# chmod-only boundary. Gate every immutable operation on this.
immutable_supported() {
    command -v chflags >/dev/null 2>&1
}

# chflags counterpart of chmod_tree_no_symlinks: never touch symlinks (so a
# secret symlinked into the tree keeps its own flags) and batch with -exec +.
chflags_tree_no_symlinks() {
    local root="$1" flags="$2"
    find -P "$root" "${GIT_LOCK_PRUNE[@]}" ! -type l -exec chflags "$flags" {} +
}

# Clear the immutable flag across the WHOLE tree, exemptions included. chmod and
# mkdir both return EPERM on a uchg path, so every lock/unlock must drop the flag
# before touching modes; lock re-applies it to the frozen paths at the end.
clear_immutable_tree() {
    immutable_supported || return 0
    chflags_tree_no_symlinks "$1" nouchg
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
        find -P "$root" "${GIT_LOCK_PRUNE[@]}" \( "${EXEMPTION_PRUNE[@]}" \) -prune -o \
            ! -type l \( -perm -u+w -o -perm -g+w -o -perm -o+w \) -print -quit | grep -q .
    else
        find -P "$root" "${GIT_LOCK_PRUNE[@]}" \
            ! -type l \( -perm -u+w -o -perm -g+w -o -perm -o+w \) -print -quit | grep -q .
    fi
}

# Set the user-immutable flag on every frozen (non-exempt) path. This is the
# belt that makes the demonstrated bypass — `chmod u+w` then write — fail at the
# OS layer: chmod itself returns EPERM on a uchg file, so an attacker must also
# run `chflags nouchg` first. Exemptions (the runtime queue) stay mutable.
set_immutable_frozen() {
    local root="$1"
    immutable_supported || return 0
    exemption_prune_expr "$root"
    if [[ ${#EXEMPTION_PRUNE[@]} -gt 0 ]]; then
        find -P "$root" "${GIT_LOCK_PRUNE[@]}" \( "${EXEMPTION_PRUNE[@]}" \) -prune -o \
            ! -type l -exec chflags uchg {} +
    else
        find -P "$root" "${GIT_LOCK_PRUNE[@]}" ! -type l -exec chflags uchg {} +
    fi
}

# True if any non-exempt path under $root lacks the immutable flag — the freeze
# is incomplete (e.g. a file created after the last lock) or an agent cleared it.
# Pairs with root_has_write_bits: a fully-locked tree must be BOTH write-bit-free
# and immutable on every frozen path. Always false where chflags is unsupported,
# so Linux satellites are judged on the chmod layer alone.
root_missing_immutable() {
    local root="$1"
    immutable_supported || return 1
    exemption_prune_expr "$root"
    if [[ ${#EXEMPTION_PRUNE[@]} -gt 0 ]]; then
        find -P "$root" "${GIT_LOCK_PRUNE[@]}" \( "${EXEMPTION_PRUNE[@]}" \) -prune -o \
            ! -type l ! -flags +uchg -print -quit | grep -q .
    else
        find -P "$root" "${GIT_LOCK_PRUNE[@]}" ! -type l ! -flags +uchg -print -quit | grep -q .
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
            if [[ "$ADMIN_FORCE" == "1" ]]; then
                echo "runtime-write-protect: ADMIN FORCE unlock for sanctioned runtime maintenance only; do not use for quick code changes: $root" >&2
            fi
            # Clear the immutable flag BEFORE chmod — chmod returns EPERM on a
            # uchg path, and deploy's git sync must be able to overwrite/delete
            # tree files. This ordering is the difference between a clean deploy
            # and a jammed CD.
            clear_immutable_tree "$root"
            chmod_tree_no_symlinks "$root" u+w
            disable_git_filemode_tracking "$root"
            echo "unlocked $root"
            ;;
        lock)
            disable_git_filemode_tracking "$root"
            # Drop any prior immutable flags first: the chmod + mkdir dance below
            # fails on a uchg path. The freeze re-applies uchg at the end, so a
            # re-lock of an already-frozen tree is idempotent.
            clear_immutable_tree "$root"
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
                # Belt: set the immutable flag so `chmod u+w` itself fails on
                # frozen paths. Best-effort by capability, but where chflags
                # exists it must take — a half-applied freeze is not locked.
                set_immutable_frozen "$root"
                if root_missing_immutable "$root"; then
                    echo "runtime-write-protect: immutable flag did NOT take on $root: uchg missing after chflags. Write bits are cleared but the boundary is weaker than intended." >&2
                    rc=1
                else
                    echo "locked $root"
                fi
            fi
            ;;
        status)
            if root_has_write_bits "$root" || root_missing_immutable "$root"; then
                echo "unlocked $root"
            else
                echo "locked $root"
            fi
            ;;
        assert-locked)
            if root_has_write_bits "$root" || root_missing_immutable "$root"; then
                echo "unlocked $root" >&2
                rc=1
            fi
            ;;
    esac
done

exit "$rc"
