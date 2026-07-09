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

## Target architecture: Token-API-owned audio artifacts

The intended end-state is for Token-API to own TTS synthesis, voice selection, and audio artifacts. Playback surfaces should eventually receive only pre-rendered audio artifacts, not text. In that model:

- Token-API applies sanitization, persona voice selection, and synthesis once, producing a durable audio artifact such as WAV.
- Queue entries can be pre-synthesized at enqueue time so dequeue/playback only has to resolve routing and deliver an already-rendered artifact.
- WSL, phone, and future Linux playback backends become audio-file players. They should not independently synthesize text or reinterpret persona voice choices.
- A single Token-API synthesis path gives all playback surfaces the same voice set, the same persona TTS behavior, and the same rendered audio for replay/debugging.
- Completion semantics move from backend TTS-engine state to audio-artifact playback state: render success proves the whole utterance was synthesized; playback success proves the artifact was played or explicitly stopped/skipped.

This target trades some enqueue-time work and storage cleanup for stronger reliability, consistent voices, replayability, and simpler execution backends. The current WSL hardening step should move WSL toward this model by making text input render to a full WAV and then playing that artifact to completion. Phone can keep its current one-utterance MacroDroid text path until an audio-artifact delivery/player path is built.

## Current phone and WSL dispatch

Token-OS dispatches one full sanitized utterance to the selected backend. Phone uses the local endpoint:

```text
/tts-chunk?session_id&playback_id&current_index&current_chunk&next_index&next_chunk&speed
```

Invariants:

- `current_chunk` is the complete sanitized utterance; `next_chunk` is empty and `next_index` is `null`.
- WSL receives the same complete utterance through `/tts/synth-and-play`; the satellite synthesizes the full text to a WAV artifact, verifies the rendered text hash, plays that WAV with no playback timeout, and Token-API records `current` as that full utterance and `next` as `null`.
- Both phone and WSL report chunk-compatible metadata with `chunks=1`, `completed_chunks=1`, one `results[0]`, `chunk_id`, and `playback_id`.
- WSL integrity checks compare `rendered_hash`/`rendered_chars` against the full utterance, not a sentence chunk. Current WSL transport is `wsl_sapi_wav_file`; pause/resume/toggle/restart are explicit unsupported-backend errors until a controllable media-player layer replaces `SoundPlayer.PlaySync()`.
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
