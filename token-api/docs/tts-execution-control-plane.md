# TTS execution control plane

Authoritative contract as of 2026-07-09:

- Token-API owns TTS session, queue compatibility metadata, current playback id, control state, ack/error state.
- Backends are execution-only: `wsl`, `phone`, later `linux`; routing order is Discord voice → WSL → phone.
- Mac `say` is removed as a TTS backend. If the active backend fails, Token-API records/returns an error; it does not fall back to Mac.
- Order remains: `sanitize -> compatibility chunking -> enqueue -> dispatch one full utterance to backend`.
- Phone and WSL both receive exactly one full utterance per message. Token-API may still sanitize/split internally for legacy contracts, but backend handoff collapses prepared chunks into one `1/1` utterance.
- WSL playback uses the SAPI text-file `/tts/speak` transport and returns `rendered_hash`/`rendered_chars` for the full utterance to guard against SAPI text truncation.
- Advisor bypass remains a Token-OS queue policy; backends receive already-sanitized chunks only.

## Control ingress

`POST /api/tts/control`

Body accepted by Token-OS:

```json
{
  "command": "pause|resume|skip|speed",
  "source": "phone_overlay",
  "backend": "phone",
  "session_id": "opaque-session-id",
  "playback_id": "opaque-playback-id",
  "speed": 1.25
}
```

`action` is accepted as a temporary alias for `command`, but phone overlay wiring should use `command`.

Processing rule: Token-OS records authoritative state first, then echoes to the active backend.

Phone echo endpoint used by Token-OS:

```text
/tts-local-control?command&session_id&playback_id&speed
```

WSL echo uses its local `/tts/control` endpoint with equivalent command semantics.

## Current phone and WSL dispatch

Token-OS dispatches one full sanitized utterance to the selected backend. Phone uses the local endpoint:

```text
/tts-chunk?session_id&playback_id&current_index&current_chunk&next_index&next_chunk&speed
```

Invariants:

- `current_chunk` is the complete sanitized utterance; `next_chunk` is empty and `next_index` is `null`.
- WSL receives the same complete utterance through `/tts/speak`; Token-API records `current` as that full utterance and `next` as `null`.
- Both phone and WSL report chunk-compatible metadata with `chunks=1`, `completed_chunks=1`, one `results[0]`, `chunk_id`, and `playback_id`.
- WSL integrity checks compare `rendered_hash`/`rendered_chars` against the full utterance, not a sentence chunk.
- Backends must not reconstruct a queue or mutate playback state before Token-OS control acknowledgement.

Token-OS also includes compatibility metadata fields such as `chunk_id`, `current_chunk_hash`, `next_chunk_hash`, and `chunk_count` for integrity/observability.

Compatibility backfill request:

```text
POST /api/tts/chunk-next
```

Body includes `session_id`, `playback_id`, `last_consumed_index`, optional `utterance_id`, and `backend`. Streaming/backfill is retired; current response is `done: true` with `reason: "streaming_retired"` and empty next-chunk fields. The endpoint remains compatibility-only for older phone/MacroDroid callers.

## Archival history: 2026-07-03 chunk/current-next model

On 2026-07-03, commit `a2aac929` / PR #574 introduced the chunk/current-next/backfill execution model. It existed to let Token-API authorize a rolling phone buffer, expose `/api/tts/chunk-next`, and make WSL follow the same per-chunk dispatch shape while still using the satellite `/tts/speak` transport.

That WSL chunk loop is now retired. WSL receives one full utterance per message, matching phone's stable one-utterance behavior. The public metadata remains chunk-compatible so older callers and tests can observe `chunk_id`, `playback_id`, `chunks`, `completed_chunks`, and `results`, but runtime execution no longer loops over sentence chunks for WSL.

## Phone events and errors

Phone posts lifecycle events to:

```text
POST /api/tts/chunk-event
```

Accepted events:

- `current_complete_next_starting`
- `buffer_drained`

Phone error forwarding:

```text
POST /api/tts/backend-error
```

Body includes `backend`, optional `session_id`, `playback_id`, `chunk_id`, `error`, optional `retryable`, and optional `detail`.
