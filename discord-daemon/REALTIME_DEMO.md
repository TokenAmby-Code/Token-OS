# Realtime Transcription Demo

This branch adds an opt-in Discord voice transcription provider:

```json
{
  "whisper_provider": "realtime-demo",
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

`voice.js` now exposes live decoded PCM frames from Discord receive streams. `transcribe.js` consumes those frames only when `whisper_provider` is `realtime-demo`. `realtime-transcriber.js` keeps one Realtime transcription session per `bot:user` stream, downsamples Discord's 48 kHz mono PCM to 24 kHz mono PCM inline, and sends `input_audio_buffer.append` events over WebSocket.

Completed transcription events reuse the existing `onTranscription` path, so Token-API voice injection does not need a separate demo route.

## Runtime Behavior

- Realtime sockets open with `?intent=transcription`; a normal realtime model socket rejects transcription session updates.
- The Discord receiver still uses the local silence detector from the chunked path. On 1.5s of Discord silence, the daemon flushes the debug PCM chunk and sends `input_audio_buffer.commit`.
- Leave/stop also commits any active user buffer before tearing down the voice stream. This matters for quick phone/gym tests where the Emperor joins, speaks, and leaves before silence fires.
- Stream-end no longer immediately closes the Realtime socket. It commits the user buffer and gives the transcription up to 20 seconds to complete before cleanup.
- Debug PCM files are still saved under `~/.discord-cli/audio/` for retry/inspection.

## Live Test Notes

- Initial working test transcribed: `Discord routing test, hello.`
- Before explicit commits, one test took about 18s because OpenAI kept the server-VAD turn open across multiple Discord chunks.
- After explicit leave-time commit, a quick join/speak/leave test completed in about 2.5s from first audio frame and about 0.5s after commit, transcribing: `forward routing.`
- Token API forwarding is healthy when the transcript reaches `onTranscription`, but the existing short-utterance debounce can buffer and then drop short standalone utterances. `forward routing.` was transcribed successfully but not forwarded because it was treated as a short buffered utterance.

### Final Live Measurements

Real-mic tests on 2026-04-29 showed the realtime path working end-to-end into the Custodes pane.

Recent realtime samples:

- `Discord voice routing test, longer this time for debounce.` completed in 5.45s from first audio frame.
- `Oh great, it worked, let me see the latency.` completed in 3.64s from first audio frame.
- `Hey, that's not very bad at all, actually.` completed in 3.90s from first audio frame.

From the demo log sample (`n=8`):

- first audio frame -> final transcript: median ~3.9s, range ~2.5s-5.9s
- commit/silence -> final transcript: median ~0.45s, range ~0.24s-0.94s

Historical OpenAI post-processing logs are not perfectly apples-to-apples because they only log `saved audio -> transcript`, not `first audio -> transcript`. For sane old `openai` pairs:

- saved audio -> transcript: median ~1.7-1.8s, p90 ~2.5s
- estimated first audio -> transcript for normal chunks: median ~4.7s (`>100KB`) to ~6.3s (`>250KB`)
- representative long utterance: ~15s captured audio + ~3.5s post-processing = ~18.5s total

The practical win is that after the system decides a turn is complete, realtime usually returns final text in about half a second instead of waiting for WAV conversion plus `/v1/audio/transcriptions`. Total perceived latency is now roughly 4s for short/medium real-mic utterances, versus about 5-6s for comparable old chunks and much worse for long post-processed chunks.

### Routing Fixes From Live Testing

The realtime path exposed two non-audio routing issues:

- `jq` was missing on the Mac, which broke shell tooling that parses Token-API instance JSON.
- `claude-cmd --instance` treated an instance with `pid=null` as missing, even if it had a valid `tmux_pane`. Codex-backed recovery sessions often have no PID recorded, so `claude-cmd` now accepts either `.pid` or `.tmux_pane` for instance resolution.

## Known Demo Limits

- Requires `OPENAI_API_KEY` or `openai_api_key` in Discord config.
- This is transcription-only, not a full speech-to-speech Realtime agent.
- It is intentionally opt-in; the production `openai` provider remains the stable fallback.
- Short standalone utterances can still be dropped by the legacy short-utterance debounce. Realtime final transcripts should probably bypass most of that debounce, keeping only explicit false-positive drops.
- The live Custodes recovery session is Codex-backed, but much of the surrounding control plane is still named `claude-*`; this should be generalized before treating the demo as production.
- Launchd was unreliable for this demo on the NAS-backed repo path. The live test daemon was run in tmux with:
  `tmux new-session -d -s discord-realtime-demo 'cd /Volumes/Imperium/Token-OS/discord-daemon && exec /opt/homebrew/bin/node daemon.js >> /Users/tokenclaw/.discord-cli/logs/tmux-realtime-demo.log 2>&1'`
