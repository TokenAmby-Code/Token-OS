# Realtime Transcription

Discord voice transcription now uses OpenAI Realtime only.

Relevant config:

```json
{
  "realtime_model": "gpt-realtime",
  "realtime_transcription_model": "gpt-4o-transcribe",
  "realtime_language": "en",
  "voice_silence_commit_ms": 700,
  "realtime_vad": {
    "threshold": 0.5,
    "prefix_padding_ms": 300,
    "silence_duration_ms": 300
  }
}
```

## Current Path

`voice.js` decodes Discord Opus receive streams to 48 kHz mono PCM and forwards frames directly to `transcribe.js` / `realtime-transcriber.js`. `realtime-transcriber.js` keeps one Realtime transcription session per `bot:user` stream, downsamples to 24 kHz mono PCM inline, and sends `input_audio_buffer.append` events over WebSocket.

There is no legacy bridge, local WAV conversion, or local audio-file retry path in the daemon anymore.

## Runtime Behavior

- Realtime sockets open with `?intent=transcription`.
- Discord silence frames feed a local commit timer.
- After `voice_silence_commit_ms` of Discord silence (default 700ms), the daemon sends `input_audio_buffer.commit`.
- Leave/stop also commits any active user buffer before tearing down the voice stream.
- Stream-end commits the user buffer and gives the transcription up to 20 seconds to complete before cleanup.

## Voice Draft Lifecycle

The daemon owns voice delivery directly. Every completed realtime transcript is
routed to tmux without consulting Token API or `claude_instances`:

- `custodes` routes by the stable tmuxctl public target `3:0`
  (`legion:custodes`) and resolves physical `%pane` only at send time;
- `mechanicus` routes by the stable tmuxctl public target `4:0`
  (`mechanicus:fabricator-general`) and resolves physical `%pane` only at send
  time;
- `imperial_guard` captures the active pane at speech start (Cadia-style active
  pane routing), converts it to a tmuxctl public target when possible, and
  resolves physical `%pane` only at send time.

Token API may still audit normal Discord messages and expose higher-level state,
but it is not in the critical path for voice transcript delivery. Stale DB rows
must not make a stable persona pane disappear.

The daemon owns the visible draft lifecycle:

- first non-command utterance creates one draft lock for `(bot_name, author_id)` and types into the target pane without Enter;
- later non-command utterances append to that same locked pane;
- standalone or suffix `ship` / `ship it` submits the locked pane;
- standalone or suffix `scratch` / `scratch that` cancels the locked pane;
- leading filler `command` is ignored for commands, e.g. `command ship`;
- `mute` temporarily server-mutes the speaking member for 15s when bot permissions allow; `unmute` clears it; `retarget` / `clear target` clears the draft lock without sending keys;
- pane titles are marked with a lock prefix while a draft is active and restored when the draft clears.

## Live Test Notes

Real-mic tests on 2026-04-29 showed the realtime path working end-to-end into the Custodes pane.

Recent realtime samples:

- `Discord voice routing test, longer this time for debounce.` completed in 5.45s from first audio frame.
- `Oh great, it worked, let me see the latency.` completed in 3.64s from first audio frame.
- `Hey, that's not very bad at all, actually.` completed in 3.90s from first audio frame.

From the realtime log sample (`n=8`):

- first audio frame -> final transcript: median ~3.9s, range ~2.5s-5.9s
- commit/silence -> final transcript: median ~0.45s, range ~0.24s-0.94s

## Known Limits

- Requires `OPENAI_API_KEY` or `openai_api_key` in Discord config.
- This is transcription-only, not a full speech-to-speech Realtime agent.
- Short standalone utterances are routed losslessly to the daemon draft lifecycle; command handling decides whether they become text or control actions.


## Latency Defaults

Defaults are intentionally fast but nonzero: `voice_silence_commit_ms = 700` and realtime VAD `silence_duration_ms = 300`. Do not set these to `0`; human micro-pauses, Discord frame jitter, and tiny audio buffers can split words into noisy fragments, reorder pending transcripts, or produce empty/buffer-too-small commits. If tuning further, treat about 400-500ms local commit and 200-250ms VAD as the aggressive floor.


## Gapless capture notes

The Discord receiver subscription stays active across commits. Local silence commits only close the current Realtime input buffer; the next real PCM frame immediately creates a fresh Realtime session and queues audio while its WebSocket becomes ready. Server VAD may also auto-commit before the local timer; the daemon treats `input_audio_buffer.committed` as a committed session so subsequent frames start the next session rather than appending to an already-committed buffer. This may create overlapping or duplicated syllables at boundaries, which is preferred over dropped speech.

## API key resolution

The Realtime transcriber (and the voice selftest probe) resolve the OpenAI key
**env-first**: `process.env.OPENAI_API_KEY || config.openai_api_key`. This
matches token-api's `_openai_api_key()` (routes/tts.py), so Discord voice and
TTS share one effective key source. The `config.json` copy is the fallback for
launchd contexts without the env var. (Full keychain migration is deferred.)
