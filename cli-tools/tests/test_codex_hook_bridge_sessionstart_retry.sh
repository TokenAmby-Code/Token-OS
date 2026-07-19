#!/usr/bin/env bash
# Behavioral-pin regression: the codex SessionStart hook POST carries a bounded
# retry so the instances-row registration lands under transient token-api load.
#
# Root cause it pins: codex has no registrar but its SessionStart hook (codex has
# no claude-side critical path, and WrapperStart is telemetry only). Before the
# fix the bridge fired SessionStart once with no --retry; a single dropped POST
# (http-000 under fleet load) enqueued to the durable outbox, which drains ONLY on
# a token-api down->up recovery edge -> the row never landed while healthy -> a
# live codex agent stayed invisible (the mechanicus:new allocation churn). The
# remedy (Option B) is a bounded synchronous retry on the registrar POST.
#
# The retry flags are built into the curl argv before curl runs, so a benign 200
# stub is enough to observe them while staying fully hermetic (no live agent, no
# outbox write). We also pin the gate: a non-registrar action stays single-shot.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BRIDGE="$ROOT/cli-tools/scripts/codex-hook-bridge.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# curl stub: log this invocation's argv, then answer 200 so the enqueue belt and
# failure-log branches never fire (keeps the test off the real durable outbox).
mkdir -p "$TMP/bin"
cat >"$TMP/bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$CURL_ARGV_LOG"
printf '200'
STUB
chmod +x "$TMP/bin/curl"

run_bridge() {
  # $1 = action type, $2 = argv-log path. Returns after the disowned POST subshell
  # has recorded its curl invocation (bounded wait; the bridge backgrounds it).
  local action="$1" log="$2"
  : >"$log"
  env \
    PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    HOME="$TMP/home" \
    TOKEN_API_URL=http://unused \
    TOKEN_API_CODEX_BRIDGE_ID=test-bridge \
    TOKEN_API_SESSION_ID=test-session \
    TOKEN_API_DISABLE_SESSION_RESUME=1 \
    CURL_ARGV_LOG="$log" \
    bash "$BRIDGE" "$action" <<<'{"session_id":"abc123"}' >/dev/null 2>&1
  local waited=0
  while [[ ! -s "$log" && "$waited" -lt 50 ]]; do
    sleep 0.1
    waited=$((waited + 1))
  done
  [[ -s "$log" ]] || { echo "FAIL: $action POST never reached curl" >&2; exit 1; }
}

ss_log="$TMP/sessionstart.argv"
run_bridge SessionStart "$ss_log"
grep -q -- '--retry' "$ss_log" || {
  echo "FAIL: SessionStart POST did not carry a bounded --retry (registrar can silently strand a live codex agent)" >&2
  cat "$ss_log" >&2
  exit 1
}
grep -q -- '/api/hooks/SessionStart' "$ss_log" || {
  echo "FAIL: SessionStart argv did not target the SessionStart hook endpoint" >&2
  exit 1
}

# Gate pin: a high-frequency, non-identity hook stays single-shot (no --retry), so
# the retry stays scoped to the registrar and is not applied fleet-wide.
stop_log="$TMP/stop.argv"
run_bridge Stop "$stop_log"
if grep -q -- '--retry' "$stop_log"; then
  echo "FAIL: non-registrar Stop POST carried --retry (retry must stay scoped to SessionStart)" >&2
  cat "$stop_log" >&2
  exit 1
fi

echo "codex-hook-bridge SessionStart retry tests passed"
