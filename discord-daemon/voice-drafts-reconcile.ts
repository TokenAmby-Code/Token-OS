// voice-drafts-reconcile.ts — three-way draft-truth reconcile.
//
// Voice draft truth lives in three places: the daemon's in-memory drafts map
// (voice-transcript-router), tmuxctld's VOICE_SESSIONS store, and token-api's
// _discord_voice_drafts dict. Boot sweeps reset all three, but a daemon that
// restarts alone (launchd KeepAlive, crash) can leave the other two holding
// orphans that silently swallow or misroute the next utterance. This endpoint
// makes the divergence observable — and, with auto_clear, heals it.
//
// Orphan classes:
// - tmuxctld_session: a VOICE_SESSIONS entry whose voice_session_id no daemon
//   draft references — cleared via /voice/session/clear by id.
// - daemon_draft: a daemon draft whose voice_session_id tmuxctld no longer
//   knows — cleared via the router (which also clears tmuxctld by owner).
// - token_api_draft: a token-api (bot, author) row with no daemon draft —
//   cleared via POST /api/discord/voice-drafts/clear.

export const RECONCILE_CONTRACT_VERSION = 'voice-drafts-reconcile.v1';

function normalizeBot(botName) {
  return String(botName || '').trim().toLowerCase().replaceAll('-', '_');
}

/**
 * @typedef {object} VoiceDraftReconciler
 * @property {function({autoClear?: boolean}=): Promise<object>} reconcile
 */

/**
 * @returns {VoiceDraftReconciler}
 */
export function createVoiceDraftReconciler({
  config,
  logger,
  voiceTranscriptRouter,
  tmuxctld,
  fetchImpl = fetch,
  tokenApiTimeoutMs = 3_000,
}) {
  async function fetchTokenApiDrafts() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), tokenApiTimeoutMs);
    try {
      const resp = await fetchImpl(
        `http://127.0.0.1:${config.token_api_port}/api/discord/voice-drafts`,
        { signal: controller.signal },
      );
      if (!resp.ok) return { ok: false, error: `HTTP ${resp.status}`, drafts: [] };
      const payload = await resp.json().catch(() => ({}));
      return { ok: true, drafts: payload.drafts || [] };
    } catch (err) {
      return { ok: false, error: err?.cause?.code || err?.name || err?.message, drafts: [] };
    } finally {
      clearTimeout(timer);
    }
  }

  async function clearTokenApiDraft(botName, authorId) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), tokenApiTimeoutMs);
    try {
      const resp = await fetchImpl(
        `http://127.0.0.1:${config.token_api_port}/api/discord/voice-drafts/clear`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bot_name: botName, author_id: authorId }),
          signal: controller.signal,
        },
      );
      return resp.ok;
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  async function reconcile({ autoClear = false } = {}) {
    const daemonDrafts = voiceTranscriptRouter?.listDrafts?.() || [];

    let tmuxctldSessions = [];
    let tmuxctldOk = true;
    let tmuxctldError = null;
    try {
      const status = await tmuxctld.voiceStatus({ timeoutMs: 5_000 });
      tmuxctldSessions = status?.sessions || [];
    } catch (err) {
      tmuxctldOk = false;
      tmuxctldError = err?.code || err?.message;
    }

    const tokenApi = await fetchTokenApiDrafts();

    const daemonSessionIds = new Set(
      daemonDrafts.map((d) => String(d.voice_session_id || '')).filter(Boolean),
    );
    const daemonOwners = new Set(
      daemonDrafts.map((d) => `${normalizeBot(d.bot_name)}:${d.author_id}`),
    );
    const tmuxctldSessionIds = new Set(
      tmuxctldSessions.map((s) => String(s.voice_session_id || '')).filter(Boolean),
    );

    const orphans = [];
    // Sources that failed to answer must not have their entries declared
    // orphaned against them — only compare against copies we actually read.
    for (const session of tmuxctldSessions) {
      const id = String(session.voice_session_id || '');
      if (id && !daemonSessionIds.has(id)) {
        orphans.push({
          source: 'tmuxctld_session',
          bot_name: normalizeBot(session.bot_name),
          author_id: String(session.user_id || ''),
          voice_session_id: id,
          cleared: false,
        });
      }
    }
    if (tmuxctldOk) {
      for (const draft of daemonDrafts) {
        const id = String(draft.voice_session_id || '');
        if (id && !tmuxctldSessionIds.has(id)) {
          orphans.push({
            source: 'daemon_draft',
            bot_name: normalizeBot(draft.bot_name),
            author_id: String(draft.author_id || ''),
            voice_session_id: id,
            cleared: false,
          });
        }
      }
    }
    if (tokenApi.ok) {
      for (const draft of tokenApi.drafts) {
        const owner = `${normalizeBot(draft.bot_name)}:${draft.author_id}`;
        if (!daemonOwners.has(owner)) {
          orphans.push({
            source: 'token_api_draft',
            bot_name: normalizeBot(draft.bot_name),
            author_id: String(draft.author_id || ''),
            voice_session_id: null,
            cleared: false,
          });
        }
      }
    }

    if (autoClear) {
      for (const orphan of orphans) {
        try {
          if (orphan.source === 'tmuxctld_session') {
            await tmuxctld.clearVoiceSession({ voiceSessionId: orphan.voice_session_id, timeoutMs: 5_000 });
            orphan.cleared = true;
          } else if (orphan.source === 'daemon_draft') {
            await voiceTranscriptRouter.clear(
              { bot: orphan.bot_name, userId: orphan.author_id },
              { timeoutMs: 5_000 },
            );
            orphan.cleared = true;
          } else if (orphan.source === 'token_api_draft') {
            orphan.cleared = await clearTokenApiDraft(orphan.bot_name, orphan.author_id);
          }
        } catch (err) {
          logger?.warn?.(
            `Voice reconcile: failed to clear ${orphan.source} ${orphan.bot_name}:${orphan.author_id}: ${err?.code || err?.message}`,
            { errorCode: 'voice_reconcile_clear_failed' },
          );
        }
      }
    }

    const report = {
      contract_version: RECONCILE_CONTRACT_VERSION,
      auto_clear: autoClear,
      counts: {
        daemon_drafts: daemonDrafts.length,
        tmuxctld_sessions: tmuxctldSessions.length,
        token_api_drafts: tokenApi.drafts.length,
      },
      sources: {
        tmuxctld: { ok: tmuxctldOk, ...(tmuxctldError ? { error: tmuxctldError } : {}) },
        token_api: { ok: tokenApi.ok, ...(tokenApi.error ? { error: tokenApi.error } : {}) },
      },
      orphans,
      in_sync: orphans.length === 0 && tmuxctldOk && tokenApi.ok,
    };
    if (orphans.length) {
      logger?.info?.(
        `Voice reconcile: ${orphans.length} orphan(s) ` +
        `(${orphans.map((o) => o.source).join(', ')}), auto_clear=${autoClear}`,
      );
    }
    return report;
  }

  return { reconcile };
}
