// voice-drafts-reconcile.test.js — pins three-way draft-truth reconcile (PR C).
//
// Fully DI'd: fake router, fake tmuxctld client, fake fetch. Never live tmux.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createVoiceDraftReconciler } from './voice-drafts-reconcile.ts';

const CONFIG = { token_api_port: 7777 };

function makeLogger(logs = []) {
  return {
    logs,
    debug(msg) { logs.push(['debug', String(msg)]); },
    info(msg) { logs.push(['info', String(msg)]); },
    warn(msg, meta) { logs.push(['warn', String(msg), meta]); },
    error(msg) { logs.push(['error', String(msg)]); },
  };
}

function makeFakes({
  daemonDrafts = [],
  tmuxctldSessions = [],
  tokenApiDrafts = [],
} = {}) {
  const routerClears = [];
  const router = {
    listDrafts: () => daemonDrafts,
    async clear(filter, opts) { routerClears.push({ filter, opts }); return []; },
  };
  const tmuxctldCalls = [];
  const tmuxctld = {
    calls: tmuxctldCalls,
    async voiceStatus() { tmuxctldCalls.push(['status']); return { sessions: tmuxctldSessions }; },
    async clearVoiceSession(args) { tmuxctldCalls.push(['clear', args]); return { cleared: 1 }; },
  };
  const fetchCalls = [];
  const fetchImpl = async (url, opts = {}) => {
    fetchCalls.push({ url: String(url), opts });
    if (String(url).endsWith('/api/discord/voice-drafts')) {
      return { ok: true, status: 200, json: async () => ({ count: tokenApiDrafts.length, drafts: tokenApiDrafts }) };
    }
    return { ok: true, status: 200, json: async () => ({ cleared: 1 }) };
  };
  return { router, routerClears, tmuxctld, fetchImpl, fetchCalls };
}

test('all three copies in sync: no orphans, in_sync true', async () => {
  const shared = { bot_name: 'mechanicus', author_id: 'op', voice_session_id: 'vs-1' };
  const fakes = makeFakes({
    daemonDrafts: [shared],
    tmuxctldSessions: [{ voice_session_id: 'vs-1', bot_name: 'mechanicus', user_id: 'op' }],
    tokenApiDrafts: [{ bot_name: 'mechanicus', author_id: 'op' }],
  });
  const reconciler = createVoiceDraftReconciler({
    config: CONFIG, logger: makeLogger(), voiceTranscriptRouter: fakes.router,
    tmuxctld: fakes.tmuxctld, fetchImpl: fakes.fetchImpl,
  });

  const report = await reconciler.reconcile();
  assert.equal(report.contract_version, 'voice-drafts-reconcile.v1');
  assert.deepEqual(report.orphans, []);
  assert.equal(report.in_sync, true);
  assert.deepEqual(report.counts, { daemon_drafts: 1, tmuxctld_sessions: 1, token_api_drafts: 1 });
});

test('classifies orphans in all three directions without clearing when auto_clear is off', async () => {
  const fakes = makeFakes({
    daemonDrafts: [{ bot_name: 'mechanicus', author_id: 'op', voice_session_id: 'vs-daemon-only' }],
    tmuxctldSessions: [{ voice_session_id: 'vs-tmux-only', bot_name: 'custodes', user_id: 'op' }],
    tokenApiDrafts: [{ bot_name: 'imperial_guard', author_id: 'op' }],
  });
  const reconciler = createVoiceDraftReconciler({
    config: CONFIG, logger: makeLogger(), voiceTranscriptRouter: fakes.router,
    tmuxctld: fakes.tmuxctld, fetchImpl: fakes.fetchImpl,
  });

  const report = await reconciler.reconcile({ autoClear: false });
  assert.deepEqual(
    report.orphans.map(o => [o.source, o.cleared]).sort(),
    [['daemon_draft', false], ['tmuxctld_session', false], ['token_api_draft', false]],
  );
  assert.equal(report.in_sync, false);
  assert.deepEqual(fakes.routerClears, [], 'no clears without auto_clear');
  assert.ok(!fakes.tmuxctld.calls.some(c => c[0] === 'clear'));
});

test('auto_clear heals each orphan through its owning surface', async () => {
  const fakes = makeFakes({
    daemonDrafts: [{ bot_name: 'mechanicus', author_id: 'op', voice_session_id: 'vs-daemon-only' }],
    tmuxctldSessions: [{ voice_session_id: 'vs-tmux-only', bot_name: 'custodes', user_id: 'u2' }],
    tokenApiDrafts: [{ bot_name: 'imperial_guard', author_id: 'u3' }],
  });
  const reconciler = createVoiceDraftReconciler({
    config: CONFIG, logger: makeLogger(), voiceTranscriptRouter: fakes.router,
    tmuxctld: fakes.tmuxctld, fetchImpl: fakes.fetchImpl,
  });

  const report = await reconciler.reconcile({ autoClear: true });
  assert.ok(report.orphans.every(o => o.cleared), 'every orphan cleared');
  const tmuxClear = fakes.tmuxctld.calls.find(c => c[0] === 'clear');
  assert.equal(tmuxClear[1].voiceSessionId, 'vs-tmux-only');
  assert.deepEqual(fakes.routerClears[0].filter, { bot: 'mechanicus', userId: 'op' });
  const tokenApiClear = fakes.fetchCalls.find(c => c.url.endsWith('/voice-drafts/clear'));
  assert.deepEqual(JSON.parse(tokenApiClear.opts.body), { bot_name: 'imperial_guard', author_id: 'u3' });
});

test('an unreachable source is reported, and its entries are never declared orphans against it', async () => {
  const fakes = makeFakes({
    daemonDrafts: [{ bot_name: 'mechanicus', author_id: 'op', voice_session_id: 'vs-1' }],
  });
  fakes.tmuxctld.voiceStatus = async () => {
    const err = new Error('tmuxctld timeout /voice/status');
    err.code = 'ETIMEDOUT';
    throw err;
  };
  const logs = [];
  const reconciler = createVoiceDraftReconciler({
    config: CONFIG, logger: makeLogger(logs), voiceTranscriptRouter: fakes.router,
    tmuxctld: fakes.tmuxctld, fetchImpl: fakes.fetchImpl,
  });

  const report = await reconciler.reconcile({ autoClear: true });
  assert.equal(report.sources.tmuxctld.ok, false);
  assert.equal(report.sources.tmuxctld.error, 'ETIMEDOUT');
  assert.ok(
    !report.orphans.some(o => o.source === 'daemon_draft'),
    'daemon drafts are not orphaned against a source that did not answer',
  );
  // token-api side still compared: daemon draft exists but token-api has no row —
  // that is a token-api-side miss, not an orphan (token-api rows are the superset check).
  assert.equal(report.in_sync, false, 'a silent source is never reported as in sync');
});
