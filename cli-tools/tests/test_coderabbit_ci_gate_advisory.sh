#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FAKE_BIN="$(mktemp -d)"
trap 'rm -rf "$FAKE_BIN"' EXIT

cat >"$FAKE_BIN/gh" <<'GH'
#!/usr/bin/env bash
if [[ "$*" == *'/check-runs'* ]]; then
  printf '%s' "${MOCK_CHECK_LINE:-}"
else
  printf '%s' "${MOCK_STATUS_LINE:-}"
fi
GH
chmod +x "$FAKE_BIN/gh"

run_case() {
  local expected="$1" status_line="${2:-}" check_line="${3:-}" output rc
  set +e
  output="$(PATH="$FAKE_BIN:$PATH" REPO=TokenAmby-Code/Token-OS SHA=deadbeef \
    MOCK_STATUS_LINE="$status_line" MOCK_CHECK_LINE="$check_line" \
    bash "$ROOT/.github/scripts/coderabbit-pr-gate.sh" 2>&1)"
  rc=$?
  set -e
  [[ "$rc" == 0 ]] || { echo "FAIL: advisory script exited $rc" >&2; exit 1; }
  [[ "$output" == *"::notice title=CodeRabbit advisory::state=$expected"* ]] || {
    echo "FAIL: expected advisory state $expected, got: $output" >&2
    exit 1
  }
  [[ "$output" != *"::error"* ]] || { echo "FAIL: emitted blocking annotation" >&2; exit 1; }
}

run_case failure $'failure\tReview rate limited'
run_case pending $'pending\tReview in progress'
run_case failure '' $'failure\tActionable findings reported'
run_case absent

echo "PASS: CodeRabbit CI status is always advisory"
