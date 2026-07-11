// tmuxctld-client.js — tiny loopback HTTP client for semantic tmuxctld ops.
//
// Discord owns Discord transport only. Pane selection, voice locks, draft
// mutation, submit/scratch/clear, and target resolution are tmuxctld-owned.

const DEFAULT_TMUXCTLD_URL = 'http://127.0.0.1:7778';
export const SERVER_HOLD_CEILING_MS = 60_000;
export const CLIENT_TIMEOUT_MARGIN_MS = 15_000;
export const LONG_HOLD_TIMEOUT_MS = SERVER_HOLD_CEILING_MS + CLIENT_TIMEOUT_MARGIN_MS;
export const DEFAULT_REQUEST_TIMEOUT_MS = 5_000;

function baseUrl() {
  return (process.env.TMUXCTLD_URL || DEFAULT_TMUXCTLD_URL).replace(/\/+$/, '');
}

function normalizeBotName(botName) {
  return String(botName || 'voice').trim().toLowerCase().replaceAll('-', '_');
}

async function request(method, path, body = null, { timeoutMs: routeTimeoutMs = null } = {}) {
  const url = `${baseUrl()}${path}`;
  // An explicit route timeout wins outright: bounded probes (health, boot
  // clears) must not be stretched by the env-wide override. The env override
  // applies only to routes that fall back to the default timeout, and can
  // only raise it — a stale short env value must not shorten requests.
  let timeoutMs;
  const explicitTimeoutMs = Number(routeTimeoutMs);
  if (routeTimeoutMs !== null && Number.isFinite(explicitTimeoutMs) && explicitTimeoutMs > 0) {
    timeoutMs = explicitTimeoutMs;
  } else {
    const configuredTimeoutMs = Number(process.env.TMUXCTLD_REQUEST_TIMEOUT_MS || DEFAULT_REQUEST_TIMEOUT_MS);
    timeoutMs = Number.isFinite(configuredTimeoutMs) && configuredTimeoutMs > 0
      ? Math.max(configuredTimeoutMs, DEFAULT_REQUEST_TIMEOUT_MS)
      : DEFAULT_REQUEST_TIMEOUT_MS;
  }
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
    // Surface the transport-level cause (ECONNREFUSED etc.) — callers need to
    // distinguish "daemon down" from "endpoint wedged" without string-matching
    // undici's generic "fetch failed".
    if (!err.code && err?.cause?.code) err.code = err.cause.code;
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
 * @property {function({voiceSessionId?: string, botName?: string, userId?: string, timeoutMs?: number}=): Promise<object>} clearVoiceSession
 * @property {function({target: string, text: string, submit?: boolean, clearPrompt?: boolean}): Promise<object>} sendText
 * @property {function(string): Promise<object>} voiceTarget
 * @property {function({voiceSessionId?: string, botName?: string, userId?: string, timeoutMs?: number}=): Promise<object>} voiceStatus
 * @property {function({timeoutMs?: number}=): Promise<object>} health
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
      }, { timeoutMs: LONG_HOLD_TIMEOUT_MS });
    },
    appendVoiceSession({ voiceSessionId, text }) {
      return request('POST', '/voice/session/append', {
        voice_session_id: voiceSessionId,
        text: String(text || ''),
      }, { timeoutMs: LONG_HOLD_TIMEOUT_MS });
    },
    shipVoiceSession({ voiceSessionId, text = '' }) {
      return request('POST', '/voice/session/ship', {
        voice_session_id: voiceSessionId,
        text: String(text || ''),
      }, { timeoutMs: LONG_HOLD_TIMEOUT_MS });
    },
    scratchVoiceSession({ voiceSessionId }) {
      return request('POST', '/voice/session/scratch', {
        voice_session_id: voiceSessionId,
      }, { timeoutMs: LONG_HOLD_TIMEOUT_MS });
    },
    clearVoiceSession({ voiceSessionId = '', botName = '', userId = '', timeoutMs = LONG_HOLD_TIMEOUT_MS } = {}) {
      return request('POST', '/voice/session/clear', {
        voice_session_id: voiceSessionId,
        bot_name: botName ? normalizeBotName(botName) : '',
        user_id: userId ? String(userId) : '',
      }, { timeoutMs });
    },
    sendText({ target, text, submit = true, clearPrompt = false }) {
      return request('POST', '/send-text', {
        pane: String(target || ''),
        text: String(text || ''),
        submit: !!submit,
        clear_prompt: !!clearPrompt,
      }, { timeoutMs: LONG_HOLD_TIMEOUT_MS });
    },
    voiceTarget(botName) {
      const query = new URLSearchParams({ bot_name: normalizeBotName(botName) });
      return request('GET', `/voice/target?${query.toString()}`);
    },
    voiceStatus({ voiceSessionId = '', botName = '', userId = '', timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS } = {}) {
      const query = new URLSearchParams();
      if (voiceSessionId) query.set('voice_session_id', voiceSessionId);
      if (botName) query.set('bot_name', normalizeBotName(botName));
      if (userId) query.set('user_id', String(userId));
      const suffix = query.size ? `?${query.toString()}` : '';
      return request('GET', `/voice/status${suffix}`, null, { timeoutMs });
    },
    health({ timeoutMs = 2_000 } = {}) {
      return request('GET', '/health', null, { timeoutMs });
    },
  };
}

export const tmuxctldClient = createTmuxctldClient();
