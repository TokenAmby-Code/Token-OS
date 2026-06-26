import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  createVoiceTranscriptRouter,
  normalizeVoiceCommand,
  parseVoiceCommand,
} from './voice-transcript-router.js';

function fakeClient(ops) {
  let seq = 0;
  return {
    async startVoiceSession({ botName, userId, channelId, routeEpoch }) {
      const id = `vs-${++seq}`;
      ops.push(['start', { botName, userId, channelId, routeEpoch, id }]);
      return { voice_session_id: id, target_role: botName === 'custodes' ? 'council:custodes' : 'palace:E' };
    },
    async appendVoiceSession({ voiceSessionId, text }) {
      ops.push(['append', voiceSessionId, text]);
      return { inserted: true, target_role: 'palace:E', utterances: ops.filter(op => op[0] === 'append').length };
    },
    async shipVoiceSession({ voiceSessionId, text }) {
      ops.push(['ship', voiceSessionId, text]);
      return { shipped: true, target_role: 'palace:E' };
    },
    async scratchVoiceSession({ voiceSessionId }) {
      ops.push(['scratch', voiceSessionId]);
      return { scratched: true, target_role: 'palace:E' };
    },
    async clearVoiceSession(args) {
      ops.push(['clear', args]);
      return { cleared: 1, sessions: [] };
    },
  };
}

test('router starts a tmuxctld voice session and appends transcript text', async () => {
  const ops = [];
  const router = createVoiceTranscriptRouter({
    logger: { warn() {}, info() {} },
    client: fakeClient(ops),
  });

  const result = await router.route({
    botName: 'imperial_guard',
    userId: 'u1',
    text: 'hold this draft',
    channelId: 'cadia',
    routeEpoch: 3,
  });

  assert.equal(result.routed, true);
  assert.equal(result.voice_session_id, 'vs-1');
  assert.deepEqual(ops, [
    ['start', { botName: 'imperial_guard', userId: 'u1', channelId: 'cadia', routeEpoch: 3, id: 'vs-1' }],
    ['append', 'vs-1', 'hold this draft'],
  ]);
});

test('router uses a voice_session_id created by voice manager without physical pane state', async () => {
  const ops = [];
  const router = createVoiceTranscriptRouter({ logger: { warn() {}, info() {} }, client: fakeClient(ops) });

  const result = await router.route({
    botName: 'imperial_guard',
    userId: 'u1',
    text: 'already locked',
    voice_session_id: 'opaque-1',
  });

  assert.equal(result.routed, true);
  assert.equal(result.voice_session_id, 'opaque-1');
  assert.deepEqual(ops, [['append', 'opaque-1', 'already locked']]);
});

test('stale voice_session_id is replaced through tmuxctld without fallback routing', async () => {
  const ops = [];
  let failed = false;
  const client = fakeClient(ops);
  const originalAppend = client.appendVoiceSession;
  client.appendVoiceSession = async (args) => {
    if (!failed && args.voiceSessionId === 'stale-1') {
      failed = true;
      const err = new Error('voice session not found');
      err.code = 'KeyError';
      throw err;
    }
    return originalAppend(args);
  };
  const router = createVoiceTranscriptRouter({ logger: { warn() {}, info() {} }, client });

  const result = await router.route({
    botName: 'imperial_guard',
    userId: 'u1',
    text: 'new words',
    voice_session_id: 'stale-1',
  });

  assert.equal(result.routed, true);
  assert.equal(result.voice_session_id, 'vs-1');
  assert.deepEqual(ops, [
    ['start', { botName: 'imperial_guard', userId: 'u1', channelId: '', routeEpoch: '', id: 'vs-1' }],
    ['append', 'vs-1', 'new words'],
  ]);
});

test('ship appends optional final text then submits through tmuxctld', async () => {
  const ops = [];
  const router = createVoiceTranscriptRouter({ logger: { warn() {}, info() {} }, client: fakeClient(ops) });

  await router.route({ botName: 'custodes', userId: 'u1', text: 'draft one' });
  const shipped = await router.route({ botName: 'custodes', userId: 'u1', text: 'final words ship it' });

  assert.equal(shipped.command, 'ship');
  assert.equal(shipped.routed, true);
  assert.deepEqual(ops.map(op => op[0]), ['start', 'append', 'ship']);
  assert.deepEqual(ops[2], ['ship', 'vs-1', 'final words']);
  assert.deepEqual(router.listDrafts(), []);
});

test('scratch cancels prompt through tmuxctld and clears local draft', async () => {
  const ops = [];
  const router = createVoiceTranscriptRouter({ logger: { warn() {}, info() {} }, client: fakeClient(ops) });

  await router.route({ botName: 'imperial_guard', userId: 'u1', text: 'draft' });
  const scratched = await router.route({ botName: 'imperial_guard', userId: 'u1', text: 'scratch that' });

  assert.equal(scratched.routed, true);
  assert.deepEqual(ops.at(-1), ['scratch', 'vs-1']);
  assert.deepEqual(router.listDrafts(), []);
});

test('clear calls tmuxctld cleanup by session id', async () => {
  const ops = [];
  const router = createVoiceTranscriptRouter({ logger: { warn() {}, info() {} }, client: fakeClient(ops) });

  await router.route({ botName: 'imperial_guard', userId: 'u1', text: 'draft' });
  const cleared = await router.clear({ bot: 'imperial_guard' });

  assert.equal(cleared.length, 1);
  assert.deepEqual(ops.at(-2), ['clear', { voiceSessionId: 'vs-1' }]);
  assert.deepEqual(ops.at(-1), ['clear', { botName: 'imperial_guard', userId: '' }]);
});

test('no target is loud and fail-closed', async () => {
  const warnings = [];
  const client = {
    async startVoiceSession() { const err = new Error('no routable attached operator client target'); err.code = 'ValueError'; throw err; },
  };
  const router = createVoiceTranscriptRouter({
    logger: { warn(msg) { warnings.push(msg); }, info() {} },
    client,
  });

  const result = await router.route({ botName: 'imperial_guard', userId: 'u1', text: 'hello' });
  assert.equal(result.routed, false);
  assert.equal(result.reason, 'no_target');
  assert.ok(warnings.some(msg => String(msg).includes('no target')));
});

test('mute/unmute remain Discord-only except optional draft append', async () => {
  const ops = [];
  const muted = [];
  const router = createVoiceTranscriptRouter({
    logger: { warn() {}, info() {} },
    client: fakeClient(ops),
    voiceManager: {
      async muteMember(userId, botName) { muted.push(['mute', userId, botName]); return { muted: true }; },
      async unmuteMember(userId, botName) { muted.push(['unmute', userId, botName]); return { unmuted: true }; },
    },
  });

  await router.route({ botName: 'custodes', userId: 'u1', text: 'draft' });
  await router.route({ botName: 'custodes', userId: 'u1', text: 'more words mute' });
  await router.route({ botName: 'custodes', userId: 'u1', text: 'unmute' });

  assert.deepEqual(muted, [['mute', 'u1', 'custodes'], ['unmute', 'u1', 'custodes']]);
  assert.deepEqual(ops.map(op => op[0]), ['start', 'append', 'append']);
});

test('late transcript after bot leave is ignored and clears stale draft', async () => {
  const ops = [];
  let connected = true;
  let listening = true;
  const router = createVoiceTranscriptRouter({
    logger: { warn() {}, info() {} },
    client: fakeClient(ops),
    voiceManager: { getStatus() { return { connected, listening }; } },
  });

  await router.route({ botName: 'imperial_guard', userId: 'u1', text: 'draft' });
  connected = false;
  listening = false;
  const result = await router.route({ botName: 'imperial_guard', userId: 'u1', text: 'late text' });

  assert.deepEqual(result, { routed: false, ignored: true, reason: 'bot_not_connected', cleared: 1 });
  assert.deepEqual(router.listDrafts(), []);
});

test('normalizes voice command text', () => {
  assert.equal(normalizeVoiceCommand('Command: Ship it!'), 'command ship it');
});

test('parses standalone commands', () => {
  assert.deepEqual(parseVoiceCommand('ship it'), { command: 'ship', draftText: '' });
  assert.deepEqual(parseVoiceCommand('scratch that'), { command: 'scratch', draftText: '' });
  assert.deepEqual(parseVoiceCommand('clear target'), { command: 'clear', draftText: '' });
});

test('parses suffix command while preserving draft text', () => {
  assert.deepEqual(parseVoiceCommand('do the thing, then verify ship'), {
    command: 'ship',
    draftText: 'do the thing, then verify',
  });
});

test('leading command filler is ignored for commands', () => {
  assert.deepEqual(parseVoiceCommand('command scratch'), { command: 'scratch', draftText: '' });
});

test('non-command transcript remains draft text', () => {
  assert.deepEqual(parseVoiceCommand('hello Custodes'), {
    command: null,
    draftText: 'hello Custodes',
  });
});
