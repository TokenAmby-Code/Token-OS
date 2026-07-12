# TTS execution control plane

Authoritative contract as of 2026-07-09:

- Token-API owns TTS session, queue compatibility metadata, current playback id, control state, ack/error state.
- Backends are execution-only: `wsl`, `phone`, later `linux`; routing order is Discord voice → WSL → phone.
- Mac `say` is removed as a TTS backend. If the active backend fails, Token-API records/returns an error; it does not fall back to Mac.
- Order is: `sanitize -> compatibility chunking -> render WAV artifact at enqueue -> dispatch one artifact URL per utterance to backend`. Token-API renders OpenAI TTS to cached WAV artifacts (keyed by voice + text hash, so repeat text is served from cache without a second OpenAI call) and mints a reachable `artifact_url` from config.
- Backends are audio-file players (`transport: openai_tts_wav_artifact`): Discord voice plays via the daemon `/voice/play`, WSL via the satellite `/audio/play`, phone via the MacroDroid `/tts-artifact` macro. None of them synthesize text or choose voices.
- Phone and WSL both receive exactly one full utterance per message. Token-API may still sanitize/split internally for legacy contracts, but backend handoff collapses prepared chunks into one `1/1` utterance.
- Advisor bypass remains a Token-OS queue policy; backends receive already-rendered artifacts only.

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

## Token-API-owned audio artifacts (implemented 2026-07-09)

Token-API owns TTS synthesis, voice selection, and audio artifacts. Playback surfaces receive only pre-rendered audio artifacts, not text:

- Token-API applies sanitization, persona voice selection, and synthesis once, producing a durable WAV artifact under the `tts-artifacts` store with sha256 + render metadata recorded.
- OpenAI synthesis sends `instructions` with a durable default that asks for brisk, direct delivery and explicitly disallows fake breathing/nonverbal breath sounds; operators may override it with `TOKEN_API_OPENAI_TTS_INSTRUCTIONS`.
- Queue entries are pre-synthesized at enqueue time so dequeue/playback only resolves routing and delivers an already-rendered artifact.
- Discord voice, WSL, phone (and future Linux) playback backends are audio-file players. They do not independently synthesize text or reinterpret persona voice choices.
- A single Token-API synthesis path gives all playback surfaces the same voice set (13 OpenAI voices, Emperor casts personas), the same persona TTS behavior, and the same rendered audio for replay/debugging.
- Completion semantics are audio-artifact playback state: render success proves the whole utterance was synthesized; playback success proves the artifact was played or explicitly stopped/skipped.
- Dispatch without a rendered artifact is a hard error (`tts_artifact_required`); there is no text fallback to a backend engine.

## Current phone and WSL dispatch

Token-OS dispatches one artifact URL per full sanitized utterance to the selected backend. Phone uses the local MacroDroid endpoint:

```text
/tts-artifact?session_id&playback_id&utterance_id&artifact_id&artifact_url&current_index&current_chunk
```

The phone macro shell-fetches `artifact_url` to local storage, plays it with `PlaySoundAction` (`waitToFinish`), and posts `buffer_drained`. See `mobile/macros/MACRODROID.md` for the on-device gotchas (MacroDroid's native save-response path is broken; the shell fetch is the transport).

Invariants:

- `current_chunk` is the complete sanitized utterance text (compatibility metadata only — the phone never speaks it); `next_chunk` is empty and `next_index` is `null`.
- WSL receives the artifact through the satellite `POST /audio/play` (`{artifact_url, artifact_id, sha256, format}`); the satellite downloads, sha256-verifies, and plays the WAV to completion, failing loud on any player error. `409` means the satellite is busy with another playback.
- Both phone and WSL report chunk-compatible metadata with `chunks=1`, `completed_chunks=1`, one `results[0]`, `chunk_id`, and `playback_id`.
- Playback is bounded by a 3600s safety timeout; pause/resume/toggle/restart remain explicit unsupported-backend errors until a controllable media-player layer lands.
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
