# Realtime Transcription

Discord voice transcription now uses OpenAI Realtime only.

Relevant config:

```json
{
  "realtime_model": "gpt-realtime",
  "realtime_transcription_model": "gpt-4o-transcribe",
  "realtime_language": "en",
  "realtime_vad": {
    "threshold": 0.5,
    "prefix_padding_ms": 300,
    "silence_duration_ms": 500
  }
}
```

## Current Path

`voice.js` decodes Discord Opus receive streams to 48 kHz mono PCM and forwards frames directly to `transcribe.js` / `realtime-transcriber.js`. `realtime-transcriber.js` keeps one Realtime transcription session per `bot:user` stream, downsamples to 24 kHz mono PCM inline, and sends `input_audio_buffer.append` events over WebSocket.

There is no legacy bridge, local WAV conversion, or local audio-file retry path in the daemon anymore.

## Runtime Behavior

- Realtime sockets open with `?intent=transcription`.
- Discord silence frames feed a local commit timer.
- After 1.5s of Discord silence, the daemon sends `input_audio_buffer.commit`.
- Leave/stop also commits any active user buffer before tearing down the voice stream.
- Stream-end commits the user buffer and gives the transcription up to 20 seconds to complete before cleanup.

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
- Short standalone utterances can still be dropped by the short-utterance debounce. Realtime final transcripts should probably bypass most of that debounce, keeping only explicit false-positive drops.
