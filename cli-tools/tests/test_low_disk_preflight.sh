#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

export HOME="$TMP/home"
mkdir -p "$HOME/.config/worktrees" "$TMP/bin" "$TMP/secrets" "$TMP/worktrees" "$TMP/protected"
export PATH="$ROOT/cli-tools/bin:$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export TOKEN_OS_MIN_FREE_KIB=2097152
export TOKEN_OS_FREE_KIB_OVERRIDE=1024
export DISPATCH_WORKTREE_DUP_CHECK=0

cat > "$TMP/bin/transplant" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$TMP/bin/transplant"

# Minimal PR-capable bare repo for worktree-setup.
git init --bare "$TMP/repo.git" >/dev/null
git -C "$TMP/repo.git" remote add origin git@github.com:TokenAmby-Code/Token-OS.git
seed="$TMP/seed"
git clone "$TMP/repo.git" "$seed" >/dev/null 2>&1
git -C "$seed" config user.email test@example.com
git -C "$seed" config user.name Test
echo ok > "$seed/README.md"
git -C "$seed" add README.md
git -C "$seed" commit -m init >/dev/null
git -C "$seed" push origin HEAD:main >/dev/null 2>&1
git --git-dir="$TMP/repo.git" symbolic-ref HEAD refs/heads/main

cat > "$HOME/.config/worktrees/Token-OS.conf" <<EOF_CONF
BARE_REPO=$TMP/repo.git
WORKTREE_PARENT=$TMP/worktrees
SECRETS_DIR=$TMP/secrets
PROTECTED_ROOT=$TMP/protected
RUNTIME_CHECKOUT=
LOCAL_BARE_MAIN_SYNC=false
EOF_CONF

set +e
out="$(worktree-setup low-disk-branch --project Token-OS --no-transplant --require-free --skip-sync 2>&1)"
rc=$?
set -e
[[ $rc -eq 75 ]] || { echo "expected worktree-setup rc 75, got $rc"; echo "$out"; exit 1; }
[[ "$out" == *"LOW DISK: refusing worktree setup"* ]] || { echo "missing low disk error"; echo "$out"; exit 1; }
[[ ! -e "$TMP/worktrees/wt-low-disk-branch" ]] || { echo "worktree was created despite low disk"; exit 1; }
[[ -z "$(git --git-dir="$TMP/repo.git" branch --list low-disk-branch)" ]] || { echo "branch was created despite low disk"; exit 1; }

set +e
out="$(dispatch --engine claude --dir "$TMP/protected" --worktree dispatch-low-disk --prompt 'low disk test' --no-gt --zealotry 3 2>&1)"
rc=$?
set -e
[[ $rc -eq 75 ]] || { echo "expected dispatch rc 75, got $rc"; echo "$out"; exit 1; }
[[ "$out" == *"LOW DISK: refusing dispatch worktree isolation"* ]] || { echo "missing dispatch low disk error"; echo "$out"; exit 1; }
[[ ! -e "$TMP/worktrees/wt-dispatch-low-disk" ]] || { echo "dispatch created worktree despite low disk"; exit 1; }
[[ -z "$(git --git-dir="$TMP/repo.git" branch --list dispatch-low-disk)" ]] || { echo "dispatch created branch despite low disk"; exit 1; }

echo "low disk preflight tests passed"
