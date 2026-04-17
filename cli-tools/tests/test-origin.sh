#!/usr/bin/env bash
# test-origin.sh — Test suite for cli-tools/lib/origin.sh
#
# Usage: bash cli-tools/tests/test-origin.sh
# Exit code: 0 if all pass, 1 otherwise.

set -u
TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$TESTS_DIR/../lib" && pwd)"

# Isolated TMPDIR so tests don't collide with live caches
export TMPDIR="$(mktemp -d -t origin-test.XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

# Reset state before each test
_reset_origin() {
    unset _IMPERIUM_ORIGIN_LOADED
    unset IMPERIUM_ORIGIN_MACHINE IMPERIUM_ORIGIN_DEVICE_ID IMPERIUM_ORIGIN_PANE
    unset IMPERIUM_ORIGIN_INSTANCE IMPERIUM_ORIGIN_GEOFENCE IMPERIUM_ORIGIN_TRANSPORT
    unset TMUX TMUX_PANE SSH_CONNECTION
    rm -f "$TMPDIR"/imperium-origin-* 2>/dev/null || true
    # shellcheck source=../lib/nas-path.sh
    source "$LIB_DIR/nas-path.sh"
    # shellcheck source=../lib/origin.sh
    source "$LIB_DIR/origin.sh"
}

PASS=0
FAIL=0
FAILED_TESTS=()

assert_eq() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        printf '[\033[32mPASS\033[0m] %s\n' "$name"
        PASS=$((PASS + 1))
    else
        printf '[\033[31mFAIL\033[0m] %s\n' "$name"
        printf '       expected: %q\n' "$expected"
        printf '       actual:   %q\n' "$actual"
        FAIL=$((FAIL + 1))
        FAILED_TESTS+=("$name")
    fi
}

assert_match() {
    local name="$1" regex="$2" actual="$3"
    if [[ "$actual" =~ $regex ]]; then
        printf '[\033[32mPASS\033[0m] %s\n' "$name"
        PASS=$((PASS + 1))
    else
        printf '[\033[31mFAIL\033[0m] %s\n' "$name"
        printf '       regex:  %s\n' "$regex"
        printf '       actual: %q\n' "$actual"
        FAIL=$((FAIL + 1))
        FAILED_TESTS+=("$name")
    fi
}

# ============================================================
# origin_machine
# ============================================================

_reset_origin
IMPERIUM_ORIGIN_MACHINE=wsl
assert_eq "override.env_var" "wsl" "$(origin_machine)"

_reset_origin
IMPERIUM_ORIGIN_MACHINE=phone
assert_eq "override.takes_precedence_over_cache" "phone" "$(
    echo 'mac' > "$TMPDIR/imperium-origin-12345.machine"
    origin_machine 12345
)"

_reset_origin
echo 'phone' > "$TMPDIR/imperium-origin-99999.machine"
assert_eq "cache.hit_returns_cached_value" "phone" "$(origin_machine 99999)"

_reset_origin
assert_eq "fallback.no_tmux_returns_self_identity" "$IMPERIUM_MACHINE" "$(origin_machine)"

_reset_origin
IMPERIUM_MACHINE=mac
assert_eq "fallback.no_tmux_with_explicit_machine" "mac" "$(origin_machine)"

# ============================================================
# _origin_machine_from_ip (pure mapping)
# ============================================================

_reset_origin
assert_eq "ip_to_machine.mac" "mac" "$(_origin_machine_from_ip 100.95.109.23)"
assert_eq "ip_to_machine.wsl" "wsl" "$(_origin_machine_from_ip 100.66.10.74)"
assert_eq "ip_to_machine.phone" "phone" "$(_origin_machine_from_ip 100.102.92.24)"
assert_eq "ip_to_machine.unknown" "unknown" "$(_origin_machine_from_ip 192.168.1.1)"

# Empty IP returns non-zero and no output
result=$(_origin_machine_from_ip "" 2>&1)
assert_eq "ip_to_machine.empty_ip_no_output" "" "$result"

# ============================================================
# origin_device_id
# ============================================================

_reset_origin
IMPERIUM_ORIGIN_MACHINE=wsl
assert_eq "device_id.from_machine_wsl" "TokenPC" "$(origin_device_id)"

_reset_origin
IMPERIUM_ORIGIN_MACHINE=mac
assert_eq "device_id.from_machine_mac" "Mac-Mini" "$(origin_device_id)"

_reset_origin
IMPERIUM_ORIGIN_DEVICE_ID=Custom-Device
assert_eq "device_id.explicit_override" "Custom-Device" "$(origin_device_id)"

# ============================================================
# origin_pane
# ============================================================

_reset_origin
TMUX_PANE='%42'
assert_eq "pane.from_tmux_pane_env" "%42" "$(origin_pane)"

_reset_origin
IMPERIUM_ORIGIN_PANE='palace:TR'
TMUX_PANE='%99'
assert_eq "pane.override_beats_env" "palace:TR" "$(origin_pane)"

# ============================================================
# origin_transport
# ============================================================

_reset_origin
assert_eq "transport.no_context_is_local" "local" "$(origin_transport)"

_reset_origin
TMUX_PANE='%1'
assert_eq "transport.tmux_pane_detected" "tmux" "$(origin_transport)"

_reset_origin
SSH_CONNECTION='100.66.10.74 44321 100.95.109.23 22'
assert_eq "transport.ssh_connection_detected" "ssh" "$(origin_transport)"

_reset_origin
IMPERIUM_ORIGIN_TRANSPORT=cron
TMUX_PANE='%1'
assert_eq "transport.override_beats_detection" "cron" "$(origin_transport)"

# ============================================================
# origin_record (JSON shape)
# ============================================================

_reset_origin
IMPERIUM_ORIGIN_MACHINE=wsl
record=$(origin_record)
assert_match "record.contains_machine_wsl" '"machine":"wsl"' "$record"
assert_match "record.contains_device_id" '"device_id":"TokenPC"' "$record"
assert_match "record.is_single_line_json" '^\{.*\}$' "$record"

# Validate JSON parses (python is guaranteed available on every Imperium machine)
if command -v python3 >/dev/null 2>&1; then
    if echo "$record" | python3 -c 'import json,sys; json.loads(sys.stdin.read())' 2>/dev/null; then
        printf '[\033[32mPASS\033[0m] %s\n' "record.valid_json"
        PASS=$((PASS + 1))
    else
        printf '[\033[31mFAIL\033[0m] %s\n' "record.valid_json"
        printf '       record: %s\n' "$record"
        FAIL=$((FAIL + 1))
        FAILED_TESTS+=("record.valid_json")
    fi
fi

# ============================================================
# origin_cache_clear
# ============================================================

_reset_origin
echo 'wsl' > "$TMPDIR/imperium-origin-777.machine"
echo '%5' > "$TMPDIR/imperium-origin-777.pane"
origin_cache_clear 777
remaining=$(ls "$TMPDIR"/imperium-origin-777.* 2>/dev/null | wc -l | tr -d ' ')
assert_eq "cache_clear.removes_all_slots" "0" "$remaining"

# ============================================================
# Summary
# ============================================================

echo
printf 'Summary: %d/%d passed\n' "$PASS" "$((PASS + FAIL))"
if (( FAIL > 0 )); then
    printf 'Failed: %s\n' "${FAILED_TESTS[*]}"
    exit 1
fi
exit 0
