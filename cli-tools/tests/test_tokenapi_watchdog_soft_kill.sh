#!/usr/bin/env bash
# Behavioral-pin regression: stale Token-API recovery asks the process to exit
# gracefully, observes the process-exit event for a bounded grace period, and
# escalates exactly once only when that explicit deadline expires.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WATCHDOG="$ROOT/cli-tools/Shell/tokenapi-watchdog"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/home/.claude"
: > "$TMP/home/.claude/token-api-heartbeat.json"

cat > "$TMP/bash-env" <<'EOF'
enable -n kill
EOF
cat > "$TMP/bin/date" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == '+%s' ]] && { echo 1000; exit 0; }
echo '2026-07-19 17:00:00'
EOF
cat > "$TMP/bin/stat" <<'EOF'
#!/usr/bin/env bash
echo 0
EOF
cat > "$TMP/bin/lsof" <<'EOF'
#!/usr/bin/env bash
echo 4242
EOF
cat > "$TMP/bin/kill" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$WATCHDOG_TEST_KILL_LOG"
if [[ "${WATCHDOG_TEST_TERM_ESRCH:-0}" == 1 && "${1:-}" == -TERM ]]; then
  printf 'kill: (%s) - No such process\n' "${2:-}" >&2
  exit 1
fi
exit "${WATCHDOG_TEST_KILL_RC:-0}"
EOF
cat > "$TMP/bin/launchctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$WATCHDOG_TEST_LAUNCHCTL_LOG"
exit 0
EOF
cat > "$TMP/bin/python3" <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
printf '%s\n' 'wait-observer' >> "$WATCHDOG_TEST_WAIT_LOG"
exit "${WATCHDOG_TEST_WAIT_RC:-0}"
EOF
chmod +x "$TMP/bin/"*

run_case() {
  local name="$1" wait_rc="$2"
  local dir="$TMP/$name"
  mkdir -p "$dir"
  : > "$dir/kills"; : > "$dir/launchctl"; : > "$dir/waits"
  set +e
  env \
    PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    HOME="$TMP/home" \
    BASH_ENV="$TMP/bash-env" \
    TOKEN_API_HEARTBEAT_FILE="$TMP/home/.claude/token-api-heartbeat.json" \
    TOKEN_API_WATCHDOG_COOLDOWN_FILE="$dir/cooldown" \
    TOKEN_API_WATCHDOG_STATE_FILE="$dir/state" \
    TOKEN_API_WATCHDOG_LOG="$dir/watchdog.log" \
    TOKEN_API_TERM_GRACE_SECONDS=10 \
    WATCHDOG_TEST_KILL_LOG="$dir/kills" \
    WATCHDOG_TEST_LAUNCHCTL_LOG="$dir/launchctl" \
    WATCHDOG_TEST_WAIT_LOG="$dir/waits" \
    WATCHDOG_TEST_WAIT_RC="$wait_rc" \
    bash "$WATCHDOG"
  rc=$?
  set -e
  printf '%s' "$rc" > "$dir/rc"
}

run_case graceful 0
[[ "$(cat "$TMP/graceful/rc")" == 0 ]]
[[ "$(cat "$TMP/graceful/kills")" == '-TERM 4242' ]]
[[ "$(wc -l < "$TMP/graceful/waits" | tr -d ' ')" == 1 ]]
if grep -q -- '-KILL\|-9' "$TMP/graceful/kills"; then
  echo 'FAIL: graceful exit escalated to SIGKILL' >&2
  exit 1
fi
grep -q 'kickstart -k gui/.*/ai.openclaw.tokenapi' "$TMP/graceful/launchctl"

run_case timeout 124
[[ "$(cat "$TMP/timeout/rc")" == 0 ]]
[[ "$(sed -n '1p' "$TMP/timeout/kills")" == '-TERM 4242' ]]
[[ "$(sed -n '2p' "$TMP/timeout/kills")" == '-KILL 4242' ]]
[[ "$(wc -l < "$TMP/timeout/kills" | tr -d ' ')" == 2 ]]
grep -q 'grace deadline 10s expired' "$TMP/timeout/watchdog.log"

run_case observer_failure 1
[[ "$(cat "$TMP/observer_failure/rc")" != 0 ]]
[[ "$(cat "$TMP/observer_failure/kills")" == '-TERM 4242' ]]
if grep -q -- '-KILL\|-9' "$TMP/observer_failure/kills"; then
  echo 'FAIL: observer failure triggered blind SIGKILL' >&2
  exit 1
fi
[[ ! -s "$TMP/observer_failure/launchctl" ]]
grep -q 'process-exit observation failed' "$TMP/observer_failure/watchdog.log"

WATCHDOG_TEST_TERM_ESRCH=1 run_case already_gone 0
[[ "$(cat "$TMP/already_gone/rc")" == 0 ]]
[[ "$(cat "$TMP/already_gone/kills")" == '-TERM 4242' ]]
[[ ! -s "$TMP/already_gone/waits" ]]
grep -q 'already gone before SIGTERM delivery' "$TMP/already_gone/watchdog.log"
grep -q 'kickstart -k gui/.*/ai.openclaw.tokenapi' "$TMP/already_gone/launchctl"

# Exercise the production kqueue observer rather than the python3 dispatch stub.
python3 -c 'import time; time.sleep(30)' & graceful_pid=$!
( sleep 0.2; kill -TERM "$graceful_pid" ) &
bash "$WATCHDOG" --wait-for-pid-exit "$graceful_pid" 2
wait "$graceful_pid" 2>/dev/null || true

python3 -c 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)' & timeout_pid=$!
sleep 0.2
set +e
bash "$WATCHDOG" --wait-for-pid-exit "$timeout_pid" 1
observer_rc=$?
set -e
[[ "$observer_rc" == 124 ]]
kill -KILL "$timeout_pid"
wait "$timeout_pid" 2>/dev/null || true

# Scope pin: only the PID returned for the Token-API port may be signaled.
if grep -R -E 'tmux|pane|wrapper' "$TMP"/*/kills; then
  echo 'FAIL: watchdog targeted an unrelated pane/wrapper process' >&2
  exit 1
fi

echo 'tokenapi-watchdog soft-kill behavioral-pin tests passed'
