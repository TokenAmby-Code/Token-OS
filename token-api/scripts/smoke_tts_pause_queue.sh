#!/usr/bin/env bash
set -euo pipefail

: "${TTS_TEST_INSTANCE_ID:?Set TTS_TEST_INSTANCE_ID to a real sender instance id}"
TOKEN_API_URL="${TOKEN_API_URL:-http://localhost:7777}"
LOG_DIR="${TOKEN_API_LOG_DIR:-${TMPDIR:-/tmp}/token-api-smoke}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/smoke_tts_pause_queue.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "[$(date -Iseconds)] smoke_tts_pause_queue start TOKEN_API_URL=$TOKEN_API_URL instance=$TTS_TEST_INSTANCE_ID"
for n in 1 2 3; do
  message="Synthetic TTS click play smoke $n"
  payload=$(jq -n --arg iid "$TTS_TEST_INSTANCE_ID" --arg msg "$message" \
    '{instance_id: $iid, message: $msg, queue_target: "pause"}')
  echo "[$(date -Iseconds)] queueing synthetic pause message $n"
  curl -sfS -X POST "$TOKEN_API_URL/api/notify/queue" \
    -H 'Content-Type: application/json' \
    -d "$payload" >/dev/null
done

echo "[$(date -Iseconds)] pause queue snapshot"
curl -sfS "$TOKEN_API_URL/api/ui/ops/state" | jq '.tts.pause_queue[] | {item_key, instance_id, message, queue}'
echo "[$(date -Iseconds)] smoke_tts_pause_queue complete log=$LOG_FILE"
