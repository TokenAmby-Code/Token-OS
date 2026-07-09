#!/usr/bin/env bash
set -euo pipefail
: "${TTS_TEST_INSTANCE_ID:?Set TTS_TEST_INSTANCE_ID to a real sender instance id}"
TOKEN_API_URL="${TOKEN_API_URL:-http://localhost:7777}"
for n in 1 2 3; do
  curl -sfS -X POST "$TOKEN_API_URL/api/notify/queue" \
    -H 'Content-Type: application/json' \
    -d "{\"instance_id\":\"$TTS_TEST_INSTANCE_ID\",\"message\":\"Synthetic TTS click play smoke $n\",\"queue_target\":\"pause\"}" >/dev/null
done
curl -sfS "$TOKEN_API_URL/api/ui/ops/state" | jq '.tts.pause_queue[] | {item_key, instance_id, message, queue}'
