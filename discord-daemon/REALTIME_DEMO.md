# Realtime Transcription Demo

This branch adds an opt-in Discord voice transcription provider:

```json
{
  "whisper_provider": "realtime-demo",
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

The existing providers stay intact:

- `openai`: current chunked PCM -> WAV -> `/v1/audio/transcriptions` path
- `wispr`: Hammerspoon/Wispr bridge path
- `realtime-demo`: live Discord PCM frames -> Realtime WebSocket transcription session

## What It Does

`voice.js` now exposes live decoded PCM frames from Discord receive streams. `transcribe.js` consumes those frames only when `whisper_provider` is `realtime-demo`. `realtime-transcriber.js` keeps one Realtime session per `bot:user` stream, resamples Discord's 48 kHz mono PCM to 24 kHz mono PCM with a persistent `ffmpeg` process, and sends `input_audio_buffer.append` events over WebSocket.

Completed transcription events reuse the existing `onTranscription` path, so Token-API voice injection does not need a separate demo route.

## Known Demo Limits

- Requires `OPENAI_API_KEY` or `openai_api_key` in Discord config.
- Uses `/opt/homebrew/bin/ffmpeg`, matching the current chunked provider.
- This is transcription-only, not a full speech-to-speech Realtime agent.
- It is intentionally opt-in; the production `openai` provider remains the stable fallback.
