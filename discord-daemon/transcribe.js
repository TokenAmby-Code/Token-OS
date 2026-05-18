// transcribe.js — Audio transcription pipeline
// Supports only the OpenAI Realtime transcription path.

import { createRealtimeTranscriber } from './realtime-transcriber.js';

/**
 * Create a transcription handler for the voice manager
 */
export function createTranscriber(config, logger) {
  logger.info('Transcriber: provider=openai-realtime');

  // Transcription result callbacks
  const resultHandlers = [];
  const realtime = createRealtimeTranscriber(config, logger, emitTranscript);

  // Short-utterance debounce: buffer 1-2 word transcriptions and prepend to next utterance.
  // Known false positives are dropped entirely. Keep this list small (<10) — the word-count
  // debounce is the real workhorse. This is just a fast path for obvious junk.
  const FALSE_POSITIVE_SOLO = new Set(['you', 'the', 'uh', 'um', 'ah', 'oh', 'bye-bye', 'bye', 'hmm']);
  const FALSE_POSITIVE_MULTI = new Set(['thank you', 'bye bye']);
  const DEBOUNCE_TIMEOUT_MS = 3000; // Pool buffered fragment into target prompt if no follow-up in 3s
  const pendingBuffers = new Map(); // keyed by `${botName}:${userId}`
  const pooledPromptBuffers = new Map(); // short fragments already typed but not submitted

  function getBufferKey(botName, userId) { return `${botName || 'unknown'}:${userId}`; }

  async function emitTranscript({ userId, text, timestamp = Date.now(), botName = null, realtime = false, ...extra }) {
    try {
      if (!text || text.trim().length === 0) {
        logger.debug('Transcriber: empty transcription, skipping');
        return;
      }

      const words = text.trim().split(/\s+/);
      const bufferKey = getBufferKey(botName, userId);

      // Short utterance (1-2 words): debounce
      if (words.length <= 2) {
        const normalized = words.map(w => w.toLowerCase().replace(/[.,!?]/g, ''));

        // Known false positive — drop entirely
        const normalizedPhrase = normalized.join(' ');
        if ((words.length === 1 && FALSE_POSITIVE_SOLO.has(normalized[0])) ||
            (words.length === 2 && FALSE_POSITIVE_MULTI.has(normalizedPhrase))) {
          logger.info(`Transcriber [${botName || 'unknown'}]: dropping false positive "${text}"`);
          return;
        }

        // Buffer short utterance, prepend to next utterance
        const existing = pendingBuffers.get(bufferKey);
        if (existing) clearTimeout(existing.timer);

        const bufferedText = existing ? `${existing.text} ${text.trim()}` : text.trim();
        const bufferedExtra = { ...extra };
        const timer = setTimeout(async () => {
          pendingBuffers.delete(bufferKey);
          pooledPromptBuffers.set(bufferKey, { text: bufferedText, timestamp: Date.now(), extra: bufferedExtra });
          logger.info(`Transcriber [${botName || 'unknown'}]: pooling short buffer into prompt "${bufferedText}"`);
          const result = {
            userId,
            text: bufferedText,
            timestamp: Date.now(),
            botName,
            realtime,
            noSubmit: true,
            pooledPrompt: true,
            ...bufferedExtra,
          };
          for (const handler of resultHandlers) {
            try { await handler(result); } catch (err) { logger.error(`Transcriber: handler error: ${err.message}`); }
          }
        }, DEBOUNCE_TIMEOUT_MS);

        pendingBuffers.set(bufferKey, { text: bufferedText, timer, extra: bufferedExtra });
        logger.info(`Transcriber [${botName || 'unknown'}]: buffered short utterance "${text.trim()}" (waiting for follow-up)`);
        return;
      }

      // Normal utterance: prepend any buffered text that has not yet been typed.
      const pending = pendingBuffers.get(bufferKey);
      let appendSubmit = false;
      if (pending) {
        clearTimeout(pending.timer);
        text = `${pending.text} ${text.trim()}`;
        pendingBuffers.delete(bufferKey);
        logger.info(`Transcriber [${botName || 'unknown'}]: prepended buffer → "${text}"`);
      } else if (pooledPromptBuffers.has(bufferKey)) {
        const pooled = pooledPromptBuffers.get(bufferKey);
        pooledPromptBuffers.delete(bufferKey);
        appendSubmit = true;
        logger.info(`Transcriber [${botName || 'unknown'}]: appending to pooled prompt "${pooled.text}" → "${text.trim()}"`);
      }

      logger.info(`Transcriber [${botName || 'unknown'}]: [${userId}] "${text}"`);

      // Notify handlers
      const result = { userId, text, timestamp, botName, realtime, appendSubmit, ...extra };
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
    handleAudioFrame(userId, pcmChunk, botName) {
      realtime.appendPCM(userId, pcmChunk, botName);
    },
    closeUser(userId, botName) {
      if (realtime) realtime.closeUser(userId, botName);
    },
    commitUser(userId, botName, meta) {
      if (realtime) realtime.commitUser(userId, botName, meta);
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
