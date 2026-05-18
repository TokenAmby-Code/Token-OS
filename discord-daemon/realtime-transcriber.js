// realtime-transcriber.js — OpenAI Realtime transcription provider
// Streams Discord PCM frames into a persistent Realtime transcription session.

import WebSocket from 'ws';

const DEFAULT_REALTIME_MODEL = 'gpt-realtime';
const DEFAULT_TRANSCRIBE_MODEL = 'gpt-4o-transcribe';
const REALTIME_URL = 'wss://api.openai.com/v1/realtime';

export function createRealtimeTranscriber(config, logger, emitTranscript) {
  const apiKey = config.openai_api_key || process.env.OPENAI_API_KEY;
  const realtimeModel = config.realtime_model || DEFAULT_REALTIME_MODEL;
  const transcriptionModel = config.realtime_transcription_model || DEFAULT_TRANSCRIBE_MODEL;
  const language = config.realtime_language || config.language || 'en';
  const prompt = config.realtime_prompt || config.prompt || '';
  const vad = config.realtime_vad || {};
  const sessions = new Map();

  function keyFor(botName, userId) {
    return `${botName || 'unknown'}:${userId}`;
  }

  function makeSession(botName, userId) {
    if (!apiKey) {
      logger.warn('Realtime: skipping — no OpenAI API key');
      return null;
    }

    const key = keyFor(botName, userId);
    const url = `${REALTIME_URL}?intent=transcription`;
    const ws = new WebSocket(url, {
      headers: { Authorization: `Bearer ${apiKey}` },
    });

    const session = {
      key,
      botName,
      userId,
      ws,
      ready: false,
      closed: false,
      firstAudioAt: null,
      appendedBytes: 0,
      appendedFrames: 0,
      startedAt: Date.now(),
      lastDeltaAt: null,
      resampleRemainder: Buffer.alloc(0),
      pendingAudio: [],
      pendingCommitMeta: null,
      lastCommitMeta: null,
      committed: false,
      cleanupTimer: null,
    };

    ws.on('open', () => {
      logger.info(`Realtime [${botName}]: connected for user ${userId}`);
      send(session, {
        type: 'session.update',
        session: {
          type: 'transcription',
          audio: {
            input: {
              format: { type: 'audio/pcm', rate: 24000 },
              noise_reduction: { type: vad.noise_reduction || 'near_field' },
              transcription: {
                model: transcriptionModel,
                language,
                prompt,
              },
              turn_detection: {
                type: 'server_vad',
                threshold: vad.threshold ?? 0.5,
                prefix_padding_ms: vad.prefix_padding_ms ?? 300,
                silence_duration_ms: vad.silence_duration_ms ?? 500,
              },
            },
          },
        },
      });
      logger.info(
        `Realtime [${botName}]: session.update sent ` +
        `(intent=transcription, transcription=${transcriptionModel}, vad=${vad.silence_duration_ms ?? 500}ms)`
      );
    });

    ws.on('message', (raw) => {
      let event;
      try {
        event = JSON.parse(raw.toString());
      } catch {
        logger.warn(`Realtime [${botName}]: non-JSON event`);
        return;
      }

      if (event.type === 'error') {
        const message = event.error?.message || JSON.stringify(event.error || event);
        logger.error(`Realtime [${botName}]: ${message}`);
        return;
      }

      if (event.type === 'session.updated') {
        logger.info(`Realtime [${botName}]: session.updated acknowledged`);
        session.ready = true;
        flushPendingAudio(session);
        if (session.pendingCommitMeta) {
          const meta = session.pendingCommitMeta;
          session.pendingCommitMeta = null;
          commitUser(userId, botName, meta);
        }
        return;
      }

      if (event.type === 'input_audio_buffer.committed') {
        logger.info(
          `Realtime [${botName}]: input committed item=${event.item_id || '?'} ` +
          `previous=${event.previous_item_id || 'none'}`
        );
        return;
      }

      if (event.type === 'input_audio_buffer.speech_started') {
        logger.debug(`Realtime [${botName}]: server VAD speech started`);
        return;
      }

      if (event.type === 'input_audio_buffer.speech_stopped') {
        logger.debug(`Realtime [${botName}]: server VAD speech stopped`);
        return;
      }

      if (event.type === 'conversation.item.input_audio_transcription.delta') {
        session.lastDeltaAt = Date.now();
        const firstAudioLatency = session.firstAudioAt ? session.lastDeltaAt - session.firstAudioAt : null;
        if (event.delta) {
          logger.debug(
            `Realtime [${botName}]: delta after ${firstAudioLatency ?? '?'}ms "${event.delta}"`
          );
        }
        return;
      }

      if (event.type === 'conversation.item.input_audio_transcription.completed') {
        const text = (event.transcript || '').trim();
        if (!text) return;
        const completedAt = Date.now();
        const firstAudioLatency = session.firstAudioAt ? completedAt - session.firstAudioAt : null;
        logger.info(
          `Realtime [${botName}]: completed after ${firstAudioLatency ?? '?'}ms ` +
          `(${session.appendedFrames} frames, ${session.appendedBytes} bytes) "${text}"`
        );
        emitTranscript({
          userId,
          text,
          timestamp: completedAt,
          botName,
          realtime: true,
          itemId: event.item_id,
          startedAt: session.startedAt,
          firstDeltaAt: session.lastDeltaAt,
          commitMeta: session.lastCommitMeta || null,
          lockedTmuxPane: session.lastCommitMeta?.lockedTmuxPane || null,
        });
        scheduleCleanup(session, 1000, 'completed');
      }
    });

    ws.on('close', (code, reason) => {
      session.closed = true;
      logger.info(`Realtime [${botName}]: closed for ${userId} (${code}) ${reason || ''}`);
      cleanupSession(session);
    });

    ws.on('error', (err) => {
      logger.error(`Realtime [${botName}]: websocket error for ${userId}: ${err.message}`);
    });

    sessions.set(key, session);
    return session;
  }

  function send(session, event) {
    if (session.ws.readyState !== WebSocket.OPEN) return false;
    session.ws.send(JSON.stringify(event));
    return true;
  }

  function appendAudio(session, audio) {
    if (!session.firstAudioAt) session.firstAudioAt = Date.now();
    session.appendedBytes += audio.length;
    session.appendedFrames++;
    if (session.appendedFrames === 1) {
      logger.info(`Realtime [${session.botName}]: first audio frame for user ${session.userId}`);
    }
    send(session, {
      type: 'input_audio_buffer.append',
      audio: audio.toString('base64'),
    });
  }

  function flushPendingAudio(session) {
    if (session.pendingAudio.length === 0) return;
    logger.info(
      `Realtime [${session.botName}]: flushing ${session.pendingAudio.length} queued audio frames`
    );
    for (const audio of session.pendingAudio.splice(0)) {
      appendAudio(session, audio);
    }
  }

  function getSession(botName, userId) {
    const key = keyFor(botName, userId);
    const existing = sessions.get(key);
    if (existing && !existing.closed && !existing.committed) return existing;
    if (existing && existing.committed && !existing.closed) {
      logger.info(`Realtime [${botName}]: starting next session while committed transcript is pending for user ${userId}`);
      sessions.delete(key);
    }
    return makeSession(botName, userId);
  }

  function downsample48kTo24k(session, pcmChunk) {
    const input = session.resampleRemainder.length
      ? Buffer.concat([session.resampleRemainder, pcmChunk])
      : pcmChunk;
    const inputBytes = input.length - (input.length % 4);
    session.resampleRemainder = inputBytes < input.length ? input.subarray(inputBytes) : Buffer.alloc(0);
    if (inputBytes === 0) return null;

    const output = Buffer.allocUnsafe(inputBytes / 2);
    let outOffset = 0;
    for (let inOffset = 0; inOffset < inputBytes; inOffset += 4) {
      const a = input.readInt16LE(inOffset);
      const b = input.readInt16LE(inOffset + 2);
      output.writeInt16LE(Math.round((a + b) / 2), outOffset);
      outOffset += 2;
    }
    return output;
  }

  function appendPCM(userId, pcmChunk, botName) {
    if (!pcmChunk || pcmChunk.length === 0) return;
    const session = getSession(botName, userId);
    if (!session || session.closed) return;
    const audio = downsample48kTo24k(session, pcmChunk);
    if (!audio || audio.length === 0) return;
    if (!session.ready) {
      session.pendingAudio.push(audio);
      if (session.pendingAudio.length > 1000) session.pendingAudio.shift();
      return;
    }
    appendAudio(session, audio);
  }

  function commitUser(userId, botName, meta = {}) {
    const session = sessions.get(keyFor(botName, userId));
    if (!session || session.closed) return false;
    if (!session.ready) {
      if (session.pendingAudio.length > 0 || session.appendedFrames > 0) {
        session.pendingCommitMeta = meta;
        logger.info(
          `Realtime [${botName}]: queued commit until ready for user ${userId} ` +
          `(pending=${session.pendingAudio.length}, reason=${meta.reason || 'manual'})`
        );
        return true;
      }
      return false;
    }
    if (session.appendedFrames === 0) return false;
    session.lastCommitMeta = meta || {};
    session.committed = true;
    logger.info(
      `Realtime [${botName}]: committing audio for user ${userId} ` +
      `(${session.appendedFrames} frames, reason=${meta.reason || 'manual'}, pane=${meta.lockedTmuxPane || 'none'})`
    );
    return send(session, { type: 'input_audio_buffer.commit' });
  }

  function scheduleCleanup(session, delayMs, reason) {
    if (session.cleanupTimer) clearTimeout(session.cleanupTimer);
    session.cleanupTimer = setTimeout(() => {
      logger.info(`Realtime [${session.botName}]: cleanup after ${reason} for user ${session.userId}`);
      cleanupSession(session);
    }, delayMs);
  }

  function cleanupSession(sessionOrKey) {
    const session = typeof sessionOrKey === 'string' ? sessions.get(sessionOrKey) : sessionOrKey;
    if (!session) return;
    if (sessions.get(session.key) === session) sessions.delete(session.key);
    if (session.cleanupTimer) clearTimeout(session.cleanupTimer);
    try { session.ws.close(); } catch {}
  }

  function closeUser(userId, botName) {
    const session = sessions.get(keyFor(botName, userId));
    if (!session) return;
    commitUser(userId, botName, { reason: 'stream-end' });
    scheduleCleanup(session, 20_000, 'stream-end');
  }

  function closeAll() {
    for (const key of sessions.keys()) cleanupSession(key);
  }

  return {
    appendPCM,
    commitUser,
    closeUser,
    closeAll,
    getStatus() {
      const out = {};
      for (const [key, session] of sessions) {
        out[key] = {
          ready: session.ready,
          closed: session.closed,
          appendedBytes: session.appendedBytes,
          appendedFrames: session.appendedFrames,
          ageMs: Date.now() - session.startedAt,
          firstAudioAgeMs: session.firstAudioAt ? Date.now() - session.firstAudioAt : null,
        };
      }
      return out;
    },
  };
}
