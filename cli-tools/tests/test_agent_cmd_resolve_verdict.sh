#!/usr/bin/env bash
# Behavioral-pin regression: agent-cmd resolve must not report a transport
# failure as a missing pane.
#
# Root: /resolve-pane runs multi-second under live load; a resolve whose
# transport RAISES (timeout / connection refused / daemon 5xx) was collapsed
# to `pane target not found` (exit 1), making a fully-registered pane look
# unaddressable (the somnium resolve FLAP). The split:
#   - transport RAISE  -> retryable transport verdict (exit 3, "resolve transport error")
#   - daemon ok:false  -> genuine missing pane   (exit 1, "pane target not found")
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENT_CMD="$ROOT/cli-tools/bin/agent-cmd"
TMP="$(mktemp -d)"
SRV_PID=""
trap 'rm -rf "$TMP"; [[ -n "$SRV_PID" ]] && kill "$SRV_PID" 2>/dev/null || true' EXIT

# --- Case 1: transport RAISE (nothing listening on :1) => retryable verdict.
set +e
out="$(TMUXCTLD_URL="http://127.0.0.1:1" "$AGENT_CMD" --pane somnium:W --resolve-only 2>&1)"
rc=$?
set -e
if [[ "$out" == *"pane target not found"* ]]; then
  echo "FAIL: transport error misreported as a missing pane: $out" >&2
  exit 1
fi
if [[ "$rc" != 3 ]]; then
  echo "FAIL: expected exit 3 (retryable transport) for a transport raise, got $rc: $out" >&2
  exit 1
fi
if [[ "$out" != *"resolve transport error"* ]]; then
  echo "FAIL: missing distinct transport verdict on stderr: $out" >&2
  exit 1
fi

# --- Case 2: daemon answers ok:false => genuine missing pane (unchanged).
python3 - "$TMP/port" <<'PY' &
import http.server
import json
import socketserver
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": False}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
with open(sys.argv[1], "w") as fh:
    fh.write(str(srv.server_address[1]))
srv.serve_forever()
PY
SRV_PID=$!

for _ in $(seq 1 50); do
  [[ -s "$TMP/port" ]] && break
  sleep 0.1
done
PORT="$(cat "$TMP/port" 2>/dev/null || true)"
[[ -n "$PORT" ]] || { echo "FAIL: fixture daemon never bound a port" >&2; exit 1; }

set +e
out2="$(TMUXCTLD_URL="http://127.0.0.1:$PORT" "$AGENT_CMD" --pane somnium:W --resolve-only 2>&1)"
rc2=$?
set -e
if [[ "$rc2" != 1 ]]; then
  echo "FAIL: expected exit 1 (missing pane) for daemon ok:false, got $rc2: $out2" >&2
  exit 1
fi
if [[ "$out2" != *"pane target not found"* ]]; then
  echo "FAIL: daemon ok:false should stay 'pane target not found': $out2" >&2
  exit 1
fi

echo "agent-cmd resolve verdict tests passed"
