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
/tts-chunk?session_id&playback_id&utterance_id&current_index&current_chunk&next_index&next_chunk&speed
```

Invariants:

- `current_chunk` and `next_chunk` are already sanitized.
- The phone starts with exactly one write-ahead chunk: current + next.
- The phone may request one replacement/backfill chunk at a time from Token-OS
  after consuming queued audio.
- The phone must not reconstruct the queue or mutate playback state before
  Token-OS control acknowledgement.
- The phone MacroDroid executor must speak only scalar locals. It must not feed
  HTTP params, dictionary derefs, or global `tts_*` state directly to
  `SpeakTextAction`.

Token-OS also includes compatibility metadata fields such as `chunk_id`, `current_chunk_hash`, `next_chunk_hash`, and `chunk_count` for integrity/observability.

### Canonical phone MacroDroid set

The active phone source set is numbered and lives under `mobile/macros/`:

| File | Macro | Endpoint / Trigger | Role |
|---|---|---|---|
| `01-controls-notification.macro` | 01 TTS Controls Notification | `/tts-control-surface` | Shows persistent pause/resume/skip/faster/stop controls. |
| `02-control-ingress.macro` | 02 TTS Control Ingress | `/tts-control` | Forwards phone control commands to Token-OS first. |
| `03-local-echo-control.macro` | 03 TTS Local Echo Control | `/tts-local-control` | Applies Token-OS-authorized local control state. |
| `04-chunk-player.macro` | 04 TTS Chunk Player | `/tts-chunk` | Performs live phone playback and in-loop chunk backfill. |
| `05-backfill-fetcher.macro` | 05 TTS Backfill Fetcher | manual helper | Diagnostic/manual helper retained in the numbered set. |
| `06-error-report.macro` | 06 TTS Error Report | `/tts-error` | Reports phone executor failures to Token-OS. |

Retired `tts-phone-*`, `tts-overlay-*`, and `90`-`94` files are not active
sources and should not be imported for this path.

### Phone playback/backfill flow

`04 TTS Chunk Player` uses MacroDroid's HTTP request dictionary named
`request`; it extracts request values into scalar locals before speaking:

```text
{lv=request[current_chunk]} -> {lv=current_chunk_text}
{lv=request[next_chunk]}    -> {lv=next_chunk_text}
```

Allowed `SpeakTextAction.m_textToSay` values:

```text
{lv=current_chunk_text}
{lv=next_chunk_text}
{lv=backfill_next_chunk}
```

After the initial current/next pair, `04` calls:

```text
POST /api/tts/chunk-next
```

with `backend`, `session_id`, `playback_id`, `utterance_id`, and
`last_consumed_index`. The response is parsed into a local dictionary and
directly dereferenced into scalar locals:

```text
{lv=backfill[next_chunk]}     -> {lv=backfill_next_chunk}
{lv=backfill[next_index]}     -> {lv=backfill_next_index}
{lv=backfill[done]}           -> {lv=backfill_done}
{lv=backfill[control_state]}  -> {lv=control_state}
```

The phone continues until Token-OS returns no next chunk, reports done/empty, or
control state stops playback. Final completion is reported as `buffer_drained`.

Do not regress to literal TTS magic text:

- no `{http_param=current_chunk}` or `{http_param=next_chunk}` in
  `SpeakTextAction`;
- no `{lv=request[...]}` or `{lv=backfill[...]}` in `SpeakTextAction`;
- no `{v=tts_*}` in `SpeakTextAction`.

## Phone events and errors

Phone posts lifecycle events to:

```text
POST /api/tts/chunk-event
```

Accepted events:

- `current_complete_next_starting`
- `buffer_drained`

`buffer_drained` may include `detail=control_stop` when local playback exits
because Token-OS returned a stop/control state.

Phone error forwarding:

```text
POST /api/tts/backend-error
```

Body includes `backend`, optional `session_id`, `playback_id`, `chunk_id`, `error`, optional `retryable`, and optional `detail`.

## Import and verification notes

MacroDroid validation is necessary but not sufficient. The import/UI path has
accepted shapes that later speak literal magic text, and `macrodroid-import`
verification can time out. After importing `01`-`06`, pull the deployed export
and inspect the live `04 TTS Chunk Player` before trusting the phone executor:

```bash
for f in mobile/macros/[0-9][0-9]-*.macro; do macrodroid-validate "$f"; done
python -m pytest -q mobile/tests/test_tts_phone_macros.py

macrodroid-state --pull
macrodroid-read /tmp/macrodroid-state/EXPORT.mdr --list | grep -E '^(01|02|03|04|05|06) TTS'
macrodroid-read /tmp/macrodroid-state/EXPORT.mdr --macro "04 TTS Chunk Player" --export-macro > /tmp/live-04.macro
macrodroid-validate /tmp/live-04.macro
```

Live smoke requires a 3+ chunk utterance. Pass criteria: every chunk speaks as
natural text, chunk events arrive, and final `buffer_drained` is emitted.
