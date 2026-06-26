// tmuxctld-client.js — tiny loopback HTTP client for semantic tmuxctld ops.
//
// Discord owns Discord transport only. Pane selection, voice locks, draft
// mutation, submit/scratch/clear, and target resolution are tmuxctld-owned.

const DEFAULT_TMUXCTLD_URL = 'http://127.0.0.1:7778';
const DEFAULT_REQUEST_TIMEOUT_MS = 5000;

function baseUrl() {
  return (process.env.TMUXCTLD_URL || DEFAULT_TMUXCTLD_URL).replace(/\/+$/, '');
}

function normalizeBotName(botName) {
  return String(botName || 'voice').trim().toLowerCase().replaceAll('-', '_');
}

async function request(method, path, body = null) {
  const url = `${baseUrl()}${path}`;
  const configuredTimeoutMs = Number(process.env.TMUXCTLD_REQUEST_TIMEOUT_MS || DEFAULT_REQUEST_TIMEOUT_MS);
  const timeoutMs = Number.isFinite(configuredTimeoutMs) && configuredTimeoutMs > 0
    ? configuredTimeoutMs
    : DEFAULT_REQUEST_TIMEOUT_MS;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
    signal: controller.signal,
  };
  if (body !== null) opts.body = JSON.stringify(body);
  try {
    const resp = await fetch(url, opts);
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const err = new Error(`tmuxctld HTTP ${resp.status} ${path}`);
      err.status = resp.status;
      err.payload = payload;
      throw err;
    }
    if (payload?.ok === false) {
      const code = payload.error?.code || 'tmuxctld_error';
      const message = payload.error?.message || code;
      const err = new Error(message);
      err.code = code;
      err.detail = payload.error?.detail;
      err.payload = payload;
      throw err;
    }
    return payload.result ?? payload;
  } catch (err) {
    if (err?.name === 'AbortError') {
      const timeoutErr = new Error(`tmuxctld timeout ${path} after ${timeoutMs}ms`);
      timeoutErr.code = 'ETIMEDOUT';
      timeoutErr.path = path;
      timeoutErr.timeoutMs = timeoutMs;
      throw timeoutErr;
    }
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}

/**
 * @typedef {object} TmuxctldClient
 * @property {function({botName: string, userId: string, channelId?: string, routeEpoch?: string|number}): Promise<object>} startVoiceSession
 * @property {function({voiceSessionId: string, text: string}): Promise<object>} appendVoiceSession
 * @property {function({voiceSessionId: string, text?: string}): Promise<object>} shipVoiceSession
 * @property {function({voiceSessionId: string}): Promise<object>} scratchVoiceSession
 * @property {function({voiceSessionId?: string, botName?: string, userId?: string}=): Promise<object>} clearVoiceSession
 * @property {function({target: string, text: string, submit?: boolean, clearPrompt?: boolean}): Promise<object>} sendText
 * @property {function(string): Promise<object>} voiceTarget
 */

/**
 * @returns {TmuxctldClient}
 */
export function createTmuxctldClient() {
  return {
    startVoiceSession({ botName, userId, channelId = '', routeEpoch = '' }) {
      return request('POST', '/voice/session/start', {
        bot_name: normalizeBotName(botName),
        user_id: String(userId || ''),
        channel_id: String(channelId || ''),
        route_epoch: String(routeEpoch ?? ''),
      });
    },
    appendVoiceSession({ voiceSessionId, text }) {
      return request('POST', '/voice/session/append', {
        voice_session_id: voiceSessionId,
        text: String(text || ''),
      });
    },
    shipVoiceSession({ voiceSessionId, text = '' }) {
      return request('POST', '/voice/session/ship', {
        voice_session_id: voiceSessionId,
        text: String(text || ''),
      });
    },
    scratchVoiceSession({ voiceSessionId }) {
      return request('POST', '/voice/session/scratch', {
        voice_session_id: voiceSessionId,
      });
    },
    clearVoiceSession({ voiceSessionId = '', botName = '', userId = '' } = {}) {
      return request('POST', '/voice/session/clear', {
        voice_session_id: voiceSessionId,
        bot_name: botName ? normalizeBotName(botName) : '',
        user_id: userId ? String(userId) : '',
      });
    },
    sendText({ target, text, submit = true, clearPrompt = false }) {
      return request('POST', '/send-text', {
        pane: String(target || ''),
        text: String(text || ''),
        submit: !!submit,
        clear_prompt: !!clearPrompt,
      });
    },
    voiceTarget(botName) {
      const query = new URLSearchParams({ bot_name: normalizeBotName(botName) });
      return request('GET', `/voice/target?${query.toString()}`);
    },
  };
}

export const tmuxctldClient = createTmuxctldClient();
