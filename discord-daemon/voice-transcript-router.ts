// voice-transcript-router.js — route Discord voice transcripts through tmuxctld.
//
// Discord remains Discord transport only. tmuxctld owns semantic target policy,
// voice locks, draft mutation, scratch/clear, and prompt submission.

import { tmuxctldClient } from './tmuxctld-client.ts';

function normalizeBot(botName) {
  return String(botName || 'unknown').trim().toLowerCase().replaceAll('-', '_');
}

export function normalizeVoiceCommand(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s]+/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

export function parseVoiceCommand(text) {
  let normalized = normalizeVoiceCommand(text);
  if (normalized.startsWith('command ')) normalized = normalized.slice('command '.length).trim();

  const commands = [
    ['scratch that', 'scratch'],
    ['reset target', 'clear'],
    ['clear target', 'clear'],
    ['clear lock', 'clear'],
    ['ship it', 'ship'],
    ['scratch', 'scratch'],
    ['retarget', 'clear'],
    ['unlock', 'clear'],
    ['unmute', 'unmute'],
    ['ship', 'ship'],
    ['mute', 'mute'],
  ];

  for (const [phrase, command] of commands) {
    if (normalized === phrase) return { command, draftText: '' };
    const suffix = ` ${phrase}`;
    if (normalized.endsWith(suffix)) {
      const words = String(text || '').trim().split(/\s+/);
      return { command, draftText: words.slice(0, -phrase.split(' ').length).join(' ').trim() };
    }
  }
  return { command: null, draftText: String(text || '').trim() };
}

/**
 * @typedef {object} VoiceTranscriptRouter
 * @property {function(object): Promise<object>} route
 * @property {function(): object[]} listDrafts
 * @property {function(object=): Promise<object[]>} clear
 */

/**
 * @returns {VoiceTranscriptRouter}
 */
export function createVoiceTranscriptRouter({
  logger,
  voiceManager = null,
  client = tmuxctldClient,
} = {}) {
  const drafts = new Map();

  function keyFor(result) {
    return {
      bot: normalizeBot(result.botName || 'voice'),
      userId: String(result.userId || 'unknown'),
      value: `${normalizeBot(result.botName || 'voice')}:${String(result.userId || 'unknown')}`,
    };
  }

  function summarizeDraft(key, state) {
    return {
      bot_name: key.bot,
      author_id: key.userId,
      voice_session_id: state.voiceSessionId,
      target_role: state.targetRole || '',
      created_at: state.createdAt,
      utterances: state.utterances || 0,
    };
  }

  async function clearDraft(key) {
    const state = drafts.get(key.value);
    if (!state) return null;
    await client.clearVoiceSession({ voiceSessionId: state.voiceSessionId });
    drafts.delete(key.value);
    return summarizeDraft(key, state);
  }

  async function clearDrafts(filter = {}) {
    const cleared = [];
    const filterBot = filter.bot ? normalizeBot(filter.bot) : null;
    const filterUserId = filter.userId ? String(filter.userId) : null;
    for (const value of [...drafts.keys()]) {
      const [bot, userId] = value.split(':', 2);
      if (filterBot && filterBot !== bot) continue;
      if (filterUserId && filterUserId !== userId) continue;
      const item = await clearDraft({ bot, userId, value });
      if (item) cleared.push(item);
    }
    // Startup/leave cleanup can run before local draft state exists, so also
    // clear tmuxctld's process-local sessions by semantic owner.
    if (filterBot || filterUserId) {
      await client.clearVoiceSession({ botName: filterBot || '', userId: filterUserId || '' });
    }
    return cleared;
  }

  async function ensureDraftSession(key, result) {
    let state = drafts.get(key.value);
    if (state) return state;

    const existingId = result.voice_session_id || result.voiceSessionId || result.commitMeta?.voice_session_id;
    if (existingId) {
      state = {
        voiceSessionId: existingId,
        targetRole: result.target_role || result.targetRole || result.commitMeta?.target_role || '',
        createdAt: new Date().toISOString(),
        utterances: 0,
      };
      drafts.set(key.value, state);
      return state;
    }

    const started = await client.startVoiceSession({
      botName: key.bot,
      userId: key.userId,
      channelId: result.channelId ?? result.commitMeta?.channelId ?? '',
      routeEpoch: result.routeEpoch ?? result.commitMeta?.routeEpoch ?? '',
    });
    state = {
      voiceSessionId: started.voice_session_id,
      targetRole: started.target_role || '',
      createdAt: new Date().toISOString(),
      utterances: 0,
    };
    drafts.set(key.value, state);
    logger?.info?.(`Voice route [${key.bot}/${key.userId}]: started ${state.voiceSessionId} target=${state.targetRole || 'unknown'}`);
    return state;
  }

  async function appendDraftText(state, draftText) {
    const result = await client.appendVoiceSession({ voiceSessionId: state.voiceSessionId, text: draftText });
    state.targetRole = result.target_role || state.targetRole || '';
    state.utterances = result.utterances ?? ((state.utterances || 0) + 1);
    return result;
  }

  function isMissingVoiceSession(err) {
    return err?.code === 'KeyError' || String(err?.message || '').includes('voice session not found');
  }

  async function route(result) {
    const key = keyFor(result);
    const text = String(result.text || '').trim();
    const parsed = parseVoiceCommand(text);
    let state = drafts.get(key.value);

    const botStatus = voiceManager?.getStatus?.(key.bot);
    if (botStatus) {
      const resultEpoch = result.routeEpoch ?? result.commitMeta?.routeEpoch;
      const resultChannelId = result.channelId ?? result.commitMeta?.channelId;
      const currentEpoch = botStatus.routeEpoch;
      const currentChannelId = botStatus.channelId ?? null;
      const epochMismatch = resultEpoch !== undefined && currentEpoch !== undefined && Number(resultEpoch) !== Number(currentEpoch);
      const channelMismatch = resultChannelId !== undefined && String(resultChannelId || '') !== String(currentChannelId || '');
      if (epochMismatch || channelMismatch) {
        const cleared = await clearDrafts({ bot: key.bot, userId: key.userId });
        logger?.warn?.(
          `Voice route [${key.bot}/${key.userId}]: ignored stale transcript ` +
          `(epoch=${resultEpoch ?? 'none'} current_epoch=${currentEpoch ?? 'none'} ` +
          `channel=${resultChannelId || 'none'} current_channel=${currentChannelId || 'none'} cleared=${cleared.length})`
        );
        return { routed: false, ignored: true, reason: 'stale_transcript', cleared: cleared.length };
      }

      if (!botStatus.connected || !botStatus.listening) {
        const cleared = await clearDrafts({ bot: key.bot, userId: key.userId });
        logger?.warn?.(
          `Voice route [${key.bot}/${key.userId}]: ignored transcript after bot left ` +
          `(connected=${!!botStatus.connected}, listening=${!!botStatus.listening}, cleared=${cleared.length})`
        );
        return { routed: false, ignored: true, reason: 'bot_not_connected', cleared: cleared.length };
      }
    }

    if (parsed.command === 'clear') {
      const cleared = await clearDraft(key);
      logger?.info?.(`Voice route [${key.bot}/${key.userId}]: clear (${cleared ? 'cleared' : 'none'})`);
      return { routed: true, command: 'clear', cleared: !!cleared };
    }

    if (parsed.command === 'scratch') {
      if (!state) return { routed: false, command: 'scratch', reason: 'no_draft' };
      try {
        await client.scratchVoiceSession({ voiceSessionId: state.voiceSessionId });
      } catch (err) {
        if (!isMissingVoiceSession(err)) throw err;
        drafts.delete(key.value);
        logger?.warn?.(`Voice route [${key.bot}/${key.userId}]: scratch session missing ${state.voiceSessionId}`);
        return {
          routed: false,
          command: 'scratch',
          reason: 'voice_session_not_found',
          voice_session_id: state.voiceSessionId,
          voice_session_invalidated: true,
        };
      }
      drafts.delete(key.value);
      logger?.info?.(`Voice route [${key.bot}/${key.userId}]: scratched ${state.voiceSessionId}`);
      return {
        routed: true,
        command: 'scratch',
        voice_session_id: state.voiceSessionId,
        target_role: state.targetRole || '',
        voice_session_invalidated: true,
      };
    }

    if (parsed.command === 'mute') {
      if (parsed.draftText && state) {
        await appendDraftText(state, parsed.draftText);
      }
      const muted = voiceManager?.muteMember
        ? await voiceManager.muteMember(key.userId, key.bot, 15_000).then(r => !!r?.muted).catch(() => false)
        : false;
      return { routed: muted, command: 'mute', muted, temporary: true, duration_ms: 15000 };
    }

    if (parsed.command === 'unmute') {
      const unmuted = voiceManager?.unmuteMember
        ? await voiceManager.unmuteMember(key.userId, key.bot).then(r => !!r?.unmuted).catch(() => false)
        : false;
      return { routed: unmuted, command: 'unmute', unmuted };
    }

    if (parsed.command === 'ship') {
      if (!state) return { routed: false, command: 'ship', reason: 'no_draft' };
      let shipped;
      try {
        shipped = await client.shipVoiceSession({ voiceSessionId: state.voiceSessionId, text: parsed.draftText || '' });
      } catch (err) {
        if (!isMissingVoiceSession(err)) throw err;
        drafts.delete(key.value);
        logger?.warn?.(`Voice route [${key.bot}/${key.userId}]: ship session missing ${state.voiceSessionId}`);
        return {
          routed: false,
          command: 'ship',
          reason: 'voice_session_not_found',
          voice_session_id: state.voiceSessionId,
          voice_session_invalidated: true,
        };
      }
      drafts.delete(key.value);
      logger?.info?.(`Voice route [${key.bot}/${key.userId}]: shipped ${state.voiceSessionId}`);
      return {
        routed: true,
        command: 'ship',
        voice_session_id: state.voiceSessionId,
        target_role: shipped.target_role || state.targetRole || '',
        voice_session_invalidated: true,
      };
    }

    if (!parsed.draftText) return { routed: false, reason: 'empty' };

    try {
      state = await ensureDraftSession(key, result);
    } catch (err) {
      logger?.warn?.(`Voice route [${key.bot}/${key.userId}]: no target (${err.code || err.message})`);
      return { routed: false, reason: 'no_target', error: err.code || err.message };
    }

    let appended;
    try {
      appended = await appendDraftText(state, parsed.draftText);
    } catch (err) {
      // Voice manager may carry an id for a session that tmuxctld already
      // cleared on a prior ship/scratch/restart. Fail closed, then create a
      // fresh semantic session through tmuxctld instead of falling back to a
      // pane or dropping the transcript silently.
      if (!isMissingVoiceSession(err)) {
        throw err;
      }
      drafts.delete(key.value);
      state = await ensureDraftSession(key, { ...result, voice_session_id: '', voiceSessionId: '', commitMeta: { ...(result.commitMeta || {}), voice_session_id: '' } });
      appended = await appendDraftText(state, parsed.draftText);
    }
    return {
      routed: true,
      drafting: true,
      voice_session_id: state.voiceSessionId,
      target_role: appended.target_role || state.targetRole || '',
    };
  }

  return {
    route,
    listDrafts() {
      return [...drafts.entries()].map(([value, state]) => {
        const [bot, userId] = value.split(':', 2);
        return summarizeDraft({ bot, userId, value }, state);
      });
    },
    async clear(filter = {}) {
      return clearDrafts(filter);
    },
  };
}
