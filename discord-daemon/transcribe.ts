// transcribe.js — Audio transcription pipeline
// Supports only the OpenAI Realtime transcription path.

import { createRealtimeTranscriber } from './realtime-transcriber.ts';

/**
 * Create a transcription handler for the voice manager
 */
export function createTranscriber(config, logger) {
  logger.info('Transcriber: provider=openai-realtime');

  // Transcription result callbacks
  const resultHandlers = [];
  const realtime = createRealtimeTranscriber(config, logger, emitTranscript);

  async function emitTranscript({ userId, text, timestamp = Date.now(), botName = null, realtime = false, ...extra }) {
    try {
      if (!text || text.trim().length === 0) {
        logger.debug('Transcriber: empty transcription, skipping');
        return;
      }

      text = text.trim();
      logger.info(`Transcriber [${botName || 'unknown'}]: [${userId}] "${text}"`);

      // Lossless forwarding: every completed transcription reaches registered
      // handlers. The daemon's voice transcript router owns the visible draft
      // lifecycle (lock/append/ship/scratch) through tmuxctld.
      const result = { userId, text, timestamp, botName, realtime, ...extra };
      for (const handler of resultHandlers) {
        try {
          await handler(result);
        } catch (err) {
          logger.error(`Transcriber: handler error: ${err.message}`);
        }
      }

      return text;
    } catch (err) {
      logger.error(`Transcriber: failed for ${userId}: ${err.message}`);
      return null;
    }
  }

  return {
    handleAudioFrame(userId, pcmChunk, botName, meta) {
      realtime.appendPCM(userId, pcmChunk, botName, meta);
    },
    closeUser(userId, botName) {
      if (realtime) realtime.closeUser(userId, botName);
    },
    commitUser(userId, botName, meta) {
      if (realtime) return realtime.commitUser(userId, botName, meta);
      return false;
    },
    dropBot(botName) {
      return realtime ? realtime.dropBot(botName) : 0;
    },
    closeAll() {
      if (realtime) realtime.closeAll();
    },
    getRealtimeStatus() {
      return realtime ? realtime.getStatus() : {};
    },
    onTranscription(handler) { resultHandlers.push(handler); },
  };
}
