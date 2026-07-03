# TTS execution control plane

Authoritative contract as of 2026-07-03:

- Token-API owns TTS session, queue, current/next chunk, playback id, control state, ack/error state.
- Backends are execution-only: `phone`, `wsl`, later `linux`.
- Mac `say` is removed as a TTS backend. If the active backend fails, Token-API records/returns an error; it does not fall back to Mac.
- Order remains: `sanitize -> chunk -> enqueue -> dispatch chunk to backend`.
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

## Phone chunk dispatch

Token-OS dispatches to the phone local endpoint:

```text
/tts-chunk?session_id&playback_id&current_index&current_chunk&next_index&next_chunk&speed
```

Invariants:

- `current_chunk` and `next_chunk` are already sanitized.
- Phone may hold exactly one write-ahead chunk: current + next.
- Phone must not loop ahead, reconstruct the queue, or mutate playback state before Token-OS control acknowledgement.

Token-OS also includes compatibility metadata fields such as `chunk_id`, `current_chunk_hash`, `next_chunk_hash`, and `chunk_count` for integrity/observability.

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
