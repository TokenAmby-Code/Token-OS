# TTS execution control plane

Authoritative contract as of 2026-07-03:

- Token-API owns TTS session, queue, current/next/backfill chunk, playback id, control state, ack/error state.
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
- Phone starts with current + next and may use MacroDroid TTS queue only for the already-authorized next/backfill chunk.
- After chunk `n` is consumed, phone requests backfill `n+2` from `POST /api/tts/chunk-next`.
- Phone must not reconstruct the full queue or mutate playback state before Token-OS control acknowledgement.

Token-OS also includes compatibility metadata fields such as `chunk_id`, `current_chunk_hash`, `next_chunk_hash`, and `chunk_count` for integrity/observability.

Backfill request:

```text
POST /api/tts/chunk-next
```

Body includes `session_id`, `playback_id`, `last_consumed_index`, optional `utterance_id`, and `backend`. Response returns `next_chunk` metadata or `done: true`; `paused` and `skipped` are explicit control-state responses.

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


## Phone MacroDroid implementation notes

The current phone macro set is numbered `01-*` through `06-*`, plus disabled `90-*` overlay retirement stubs. The control notification uses MacroDroid 5.65 direct notification-button action fields (`actionClassType`, `actionName`, `actionJson`) to run a single local `HttpRequestAction` per button. It deliberately avoids the older `NotificationButtonTrigger`/macro-reference shape that imported as invalidly configured. Import through `MACRODROID_AUTO_IMPORT=1 macrodroid-import`; the operator still approves MacroDroid’s import prompt on the phone.

Backfill parsing deliberately avoids `IterateDictionaryAction`; the helper parses `/api/tts/chunk-next` with `JsonParseAction` and assigns known fields directly. Validate by MacroDroid import/export and pulled `.mdr` shape, not by `macrodroid-validate` alone.
