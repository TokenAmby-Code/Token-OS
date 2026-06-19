#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TEMPLATE="$ROOT/mobile/termux-bashrc-template"
STARTUP_BLOCK=$(mktemp)
MAC_BLOCK=$(mktemp)
sed -n '/^# STARTUP \/ MAC INSISTENCE/,$p' "$TEMPLATE" > "$STARTUP_BLOCK"
sed -n '/^mac() {/,/^# wsl:/p' "$TEMPLATE" | sed '$d' > "$MAC_BLOCK"

run_case() {
    local name="$1" setup="$2" expected_count="$3" expected_cooldown="$4"
    local tmp script
    tmp=$(mktemp -d)
    script="$tmp/case.sh"
    cat > "$script" <<EOF
set -e
HOME="$tmp"
unset TMUX SSH_CONNECTION
TERMUX_VERSION=0.118
$setup
source "$STARTUP_BLOCK"
mac() { echo mac >> "\$HOME/mac.calls"; return 0; }
_termux_prompt_auto_mac || true
calls=0
[[ -f "\$HOME/mac.calls" ]] && calls=\$(wc -l < "\$HOME/mac.calls" | tr -d ' ')
[[ "\$calls" == "$expected_count" ]] || { echo "$name: expected $expected_count calls, got \$calls" >&2; exit 1; }
if [[ "$expected_cooldown" == yes ]]; then
  [[ -f "\$HOME/.mac-auto-cooldown" ]] || { echo "$name: expected cooldown" >&2; exit 1; }
else
  [[ ! -f "\$HOME/.mac-auto-cooldown" ]] || { echo "$name: unexpected cooldown" >&2; exit 1; }
fi
EOF
    bash -i "$script" >/dev/null 2>"$tmp/err" || { cat "$tmp/err" >&2; return 1; }
    rm -rf "$tmp"
}

run_case "bare prompt" "" 1 no
run_case "tmux skips" "TMUX=/tmp/tmux" 0 no
run_case "ssh skips" "SSH_CONNECTION='1 2 3 4'" 0 no
run_case "not-mac skips" "touch \"\$HOME/.not-mac\"" 0 no
run_case "cooldown skips once" "touch \"\$HOME/.mac-auto-cooldown\"" 0 no

# not-mac persists opt-out; explicit mac removes the marker before attempting ssh.
tmp=$(mktemp -d)
script="$tmp/not-mac.sh"
cat > "$script" <<EOF
set -e
HOME="$tmp"
unset TMUX SSH_CONNECTION
TERMUX_VERSION=0.118
source "$STARTUP_BLOCK"
source "$MAC_BLOCK"
is_portable_monitor() { return 1; }
not-mac >/dev/null
[[ -f "\$HOME/.not-mac" ]] || exit 1
ssh() { return 255; }
mac || true
[[ ! -f "\$HOME/.not-mac" ]] || { echo "mac did not clear .not-mac" >&2; exit 1; }
EOF
bash -i "$script" >/dev/null
rm -rf "$tmp"
# Clean mac return arms one-prompt cooldown.
tmp=$(mktemp -d)
script="$tmp/cooldown.sh"
cat > "$script" <<EOF
set -e
HOME="$tmp"
unset TMUX SSH_CONNECTION
TERMUX_VERSION=0.118
source "$MAC_BLOCK"
is_portable_monitor() { return 1; }
ssh() { return 0; }
mac
[[ -f "\$HOME/.mac-auto-cooldown" ]] || { echo "mac did not arm cooldown" >&2; exit 1; }
EOF
bash -i "$script" >/dev/null
rm -rf "$tmp"
rm -f "$STARTUP_BLOCK" "$MAC_BLOCK"
