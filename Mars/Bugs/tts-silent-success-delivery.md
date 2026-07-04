# Bug: TTS false-positive delivery returns success with no audible output

- Status: fixed in branch `investigate/tts-silent-success-delivery`
- Opened: 2026-07-04
- Severity: high — enforcement channel false-positive delivery
- Affected command/path: `tts` CLI → `/api/notify` / `/api/notify/queue` → `speak_tts` → phone backend

## Symptom

During a `phone_distraction_blocked` hook, `tts "Phone's blocked..."` returned exit 0 twice, including `--verbose`, but no device played audible audio.

## Root cause

Code contract bug, not a Mac fallback/no-op:

1. Live routing selected `phone` after Discord voice was disconnected and WSL satellite was unavailable. The phone MacroDroid endpoint was reachable, so the router sent the chunk to the phone backend.
2. `_send_phone_tts_chunk` accepted the phone HTTP transport result and waited for the phone `buffer_drained`/playback callback, but a watchdog timeout was still returned as `success: true` with `playback_confirmed: false`.
3. `dispatch_tts_chunks_to_backend` propagated that as a successful `phone` route, so `speak_tts` could claim delivery without the authoritative playback ack.
4. The `tts` CLI also defaulted to fire-and-forget `/api/notify/queue`, discarded the response, and returned 0 when `curl` was spawned. That made queue/transport acceptance indistinguishable from audible delivery.

Mac backend removal was not the observed silent fallthrough: `speak_tts_mac` is removed/fail-error and the routing chain did not silently no-op through Mac.

## Fix

- Phone backend now treats a missed playback callback/watchdog timeout as delivery failure:
  - `success: false`
  - `reason/error: phone_playback_unconfirmed`
  - `playback_confirmed: false`
  - preserves original transport acceptance as `transport_success` for diagnostics.
- Phone callback completion now sets the pending waiter by `playback_id` even when no stream `session_id` is present.
- `tts` CLI now synchronously calls `/api/notify` by default and exits 0 only when Token-API reports audio delivery (`delivered`, `audio_delivered`, or `tts.audio_delivered`).
- Queue completion now carries backend `reason`/`error` into `/api/notify` responses so verbose callers see why delivery failed.
- Fire-and-forget behavior remains available only via explicit `--async`/`--fire-and-forget`, documented as request-spawn only.

## Regression tests

- `token-api/tests/test_comms_router.py`
  - missed phone callback yields non-delivery, not success
  - phone chunk target includes playback id
  - confirmed `buffer_drained` callback is the success path
- `cli-tools/tests/test_tts_cli_delivery_contract.py`
  - CLI exits nonzero when API reports no audio
  - CLI uses `/api/notify`, not `/api/notify/queue`
  - CLI exits zero only on reported audio delivery

## Proof commands

```bash
python -m pytest -o addopts='' \
  token-api/tests/test_comms_router.py::test_phone_direct_tts_only_occurs_inside_the_router \
  token-api/tests/test_comms_router.py::test_phone_tts_targets_chunk_endpoint_with_playback_id \
  token-api/tests/test_comms_router.py::test_phone_watchdog_advances_on_missed_callback \
  token-api/tests/test_comms_router.py::test_phone_playback_confirmed_when_buffer_drained_arrives \
  cli-tools/tests/test_tts_cli_delivery_contract.py -q
```

Expected: all pass after the fix; phone missed-callback expectations fail red before the router patch and the CLI non-delivery test fails red before the CLI patch.
