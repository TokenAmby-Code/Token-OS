import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  createVoiceTranscriptRouter,
  defaultVoiceTargetForBot,
  normalizeVoiceCommand,
  parseVoiceCommand,
  resolveStaticVoiceTargetToPane,
  selectInitialVoiceTarget,
} from './voice-transcript-router.js';


test('Custodes static resolver uses main:3 marker even when other panes exist', () => {
  const calls = [];
  const execSync = (cmd, args) => {
    calls.push([cmd, args]);
    assert.equal(cmd, 'tmux');
    assert.equal(args[0], 'list-panes');
    return [
      'main	3	1	%8	',
      'main	3	0	%9	legion:custodes',
      'main	1	0	%42	imperial_guard:cadia',
    ].join('\n');
  };

  const pane = resolveStaticVoiceTargetToPane('3:0', {
    execSync,
    paneExistsFn: p => p === '%9' || p === '%8' || p === '%42',
  });

  assert.equal(pane, '%9');
  assert.equal(calls.length, 1);
});

test('Mechanicus static resolver uses main:4 Fabricator-General marker', () => {
  const pane = resolveStaticVoiceTargetToPane('4:0', {
    execSync: () => [
      'main	4	2	%10	',
      'main	4	0	%11	mechanicus:fabricator-general',
      'main	3	0	%9	legion:custodes',
    ].join('\n'),
    paneExistsFn: () => true,
  });

  assert.equal(pane, '%11');
});

test('static resolver falls back only to first live pane in persona window', () => {
  const pane = resolveStaticVoiceTargetToPane('3:0', {
    execSync: () => [
      'main	1	0	%42	imperial_guard:cadia',
      'main	3	1	%8	',
      'main	3	0	%9	wrong-marker',
    ].join('\n'),
    paneExistsFn: p => p === '%8',
  });

  assert.equal(pane, '%8');
});

test('persona bots use stable tmuxctl public targets, not physical pane ids', () => {
  assert.equal(defaultVoiceTargetForBot('custodes'), '3:0');
  assert.equal(defaultVoiceTargetForBot('mechanicus'), '4:0');
  assert.equal(defaultVoiceTargetForBot('fabricator-general'), '4:0');
  assert.equal(defaultVoiceTargetForBot('fg'), '4:0');
});

test('persona initial targets ignore Cadia active-pane locks', () => {
  assert.equal(selectInitialVoiceTarget('custodes', '%99'), '3:0');
  assert.equal(selectInitialVoiceTarget('mechanicus', '%99'), '4:0');
  assert.equal(selectInitialVoiceTarget('fabricator-general', '%99'), '4:0');
  assert.equal(selectInitialVoiceTarget('imperial_guard', '%99'), '%99');
});

test('unavailable persona static target fails instead of falling back to locked pane', async () => {
  const router = createVoiceTranscriptRouter({
    logger: { warn() {}, info() {} },
    resolveTargetToPane() { return null; },
  });

  const result = await router.route({
    botName: 'custodes',
    userId: 'u1',
    text: 'hello Terra',
    lockedTmuxPane: '%99',
  });

  assert.deepEqual(result, { routed: false, reason: 'no_target' });
});

test('Imperial Guard clear restores Cadia overlay ownership for its own draft', async () => {
  const ops = [];
  const router = createVoiceTranscriptRouter({
    logger: { warn() {}, info() {} },
    resolveTargetToPane(target) { return target === '%42' ? '%42' : null; },
    displayValue() { return 'old-title'; },
    async setPaneTitle(target, title) { ops.push(['title', target, title]); },
    async setPaneOption(target, option, value) { ops.push(['option', target, option, value]); },
    async typeIntoTarget(target, text) { ops.push(['type', target, text]); },
    lockedPaneTarget(result) { return result.lockedTmuxPane; },
  });

  const first = await router.route({
    botName: 'imperial_guard',
    userId: 'u1',
    text: 'hold this draft',
    lockedTmuxPane: '%42',
  });
  assert.equal(first.routed, true);

  const cleared = await router.clear({ bot: 'imperial_guard' });
  assert.equal(cleared.length, 1);

  assert.deepEqual(ops, [
    ['title', '%42', 'IG🔒 old-title'],
    ['option', '%42', '@DISCORD_VOICE_LOCK', '1'],
    ['option', '%42', '@DISCORD_VOICE_PROCESSING', '0'],
    ['option', '%42', '@DISCORD_VOICE_PROCESSING', '1'],
    ['type', '%42', 'hold this draft'],
    ['option', '%42', '@DISCORD_VOICE_PROCESSING', '0'],
    ['title', '%42', 'old-title'],
    ['option', '%42', '@DISCORD_VOICE_PROCESSING', '0'],
    ['option', '%42', '@DISCORD_VOICE_LOCK', '0'],
  ]);
});


test('Imperial Guard cleanup clears voice lock even if processing clear fails', async () => {
  const ops = [];
  let clearing = false;
  const router = createVoiceTranscriptRouter({
    logger: { warn() {}, info() {} },
    resolveTargetToPane(target) { return target === '%42' ? '%42' : null; },
    displayValue() { return 'old-title'; },
    async setPaneTitle() {},
    async setPaneOption(target, option, value) {
      ops.push(['option', target, option, value]);
      if (clearing && option === '@DISCORD_VOICE_PROCESSING') throw new Error('processing clear failed');
    },
    async typeIntoTarget() {},
    lockedPaneTarget(result) { return result.lockedTmuxPane; },
  });

  await router.route({ botName: 'imperial_guard', userId: 'u1', text: 'draft', lockedTmuxPane: '%42' });
  clearing = true;
  const cleared = await router.clear({ bot: 'imperial_guard' });

  assert.equal(cleared.length, 1);
  assert.deepEqual(ops.slice(-2), [
    ['option', '%42', '@DISCORD_VOICE_PROCESSING', '0'],
    ['option', '%42', '@DISCORD_VOICE_LOCK', '0'],
  ]);
});

test('a flaky processing-flag write never blocks transcript delivery', async () => {
  const typed = [];
  const router = createVoiceTranscriptRouter({
    logger: { warn() {}, info() {} },
    resolveTargetToPane(target) { return target === '%42' ? '%42' : null; },
    displayValue() { return 'old-title'; },
    async setPaneTitle() {},
    async setPaneOption(_target, option) {
      // Lock acquire works; only the cosmetic processing edges flake.
      if (option === '@DISCORD_VOICE_PROCESSING') throw new Error('set-option flaked');
    },
    async typeIntoTarget(target, text) { typed.push([target, text]); },
    lockedPaneTarget(result) { return result.lockedTmuxPane; },
  });

  const result = await router.route({
    botName: 'imperial_guard',
    userId: 'u1',
    text: 'must still land',
    lockedTmuxPane: '%42',
  });

  assert.equal(result.routed, true);
  assert.deepEqual(typed, [['%42', 'must still land']]);
  assert.equal(router.listDrafts().length, 1);
});

test('voice processing flag is cleared even when typing into the pane fails', async () => {
  const ops = [];
  const router = createVoiceTranscriptRouter({
    logger: { warn() {}, info() {} },
    resolveTargetToPane(target) { return target === '%42' ? '%42' : null; },
    displayValue() { return 'old-title'; },
    async setPaneTitle() {},
    async setPaneOption(target, option, value) { ops.push(['option', target, option, value]); },
    async typeIntoTarget() { throw new Error('pane write failed'); },
    lockedPaneTarget(result) { return result.lockedTmuxPane; },
  });

  await assert.rejects(
    router.route({ botName: 'imperial_guard', userId: 'u1', text: 'draft', lockedTmuxPane: '%42' }),
    /pane write failed/
  );

  // First-utterance failure tears the draft down: processing cleared, lock released.
  assert.deepEqual(router.listDrafts(), []);
  assert.deepEqual(ops.slice(-3), [
    ['option', '%42', '@DISCORD_VOICE_PROCESSING', '0'],
    ['option', '%42', '@DISCORD_VOICE_PROCESSING', '0'],
    ['option', '%42', '@DISCORD_VOICE_LOCK', '0'],
  ]);
});

test('clearing a persona bot leaves Cadia drafts intact', async () => {
  const typed = [];
  const router = createVoiceTranscriptRouter({
    logger: { warn() {}, info() {} },
    resolveTargetToPane(target) { return target === '3:0' || target === '%42' ? target : null; },
    displayValue() { return 'old-title'; },
    async setPaneTitle() {},
    async setPaneOption() {},
    async typeIntoTarget(target, text) { typed.push([target, text]); },
    lockedPaneTarget(result) { return result.lockedTmuxPane; },
  });

  await router.route({ botName: 'custodes', userId: 'u1', text: 'terra draft', lockedTmuxPane: '%42' });
  await router.route({ botName: 'imperial_guard', userId: 'u1', text: 'cadia draft', lockedTmuxPane: '%42' });

  const cleared = await router.clear({ bot: 'custodes' });
  assert.equal(cleared.length, 1);
  assert.deepEqual(router.listDrafts().map(d => d.bot_name), ['imperial_guard']);
  assert.deepEqual(typed, [['3:0', 'terra draft'], ['%42', 'cadia draft']]);
});

test('late transcript after bot leave is ignored and clears stale draft', async () => {
  let connected = true;
  let listening = true;
  const router = createVoiceTranscriptRouter({
    logger: { warn() {}, info() {} },
    voiceManager: {
      getStatus() { return { connected, listening }; },
    },
    resolveTargetToPane(target) { return target === '%42' ? '%42' : null; },
    displayValue() { return 'old-title'; },
    async setPaneTitle() {},
    async setPaneOption() {},
    async typeIntoTarget() {},
    lockedPaneTarget(result) { return result.lockedTmuxPane; },
  });

  await router.route({ botName: 'imperial_guard', userId: 'u1', text: 'cadia draft', lockedTmuxPane: '%42' });
  assert.equal(router.listDrafts().length, 1);

  connected = false;
  listening = false;
  const result = await router.route({ botName: 'imperial_guard', userId: 'u1', text: 'late stale text', lockedTmuxPane: '%42' });

  assert.deepEqual(result, { routed: false, ignored: true, reason: 'bot_not_connected', cleared: 1 });
  assert.deepEqual(router.listDrafts(), []);
});


test('late transcript with stale epoch/channel is ignored and clears Cadia lock draft', async () => {
  const ops = [];
  const warnings = [];
  let status = { connected: true, listening: true, channelId: 'cadia', routeEpoch: 1 };
  const router = createVoiceTranscriptRouter({
    logger: { warn(msg) { warnings.push(msg); }, info() {} },
    voiceManager: { getStatus() { return status; } },
    resolveTargetToPane(target) { return target === '%42' ? '%42' : null; },
    displayValue() { return 'old-title'; },
    async setPaneTitle(target, title) { ops.push(['title', target, title]); },
    async setPaneOption(target, option, value) { ops.push(['option', target, option, value]); },
    async typeIntoTarget(target, text) { ops.push(['type', target, text]); },
    lockedPaneTarget(result) { return result.lockedTmuxPane; },
  });

  await router.route({
    botName: 'imperial_guard',
    userId: 'u1',
    text: 'cadia draft',
    lockedTmuxPane: '%42',
    routeEpoch: 1,
    channelId: 'cadia',
  });
  assert.equal(router.listDrafts().length, 1);

  status = { connected: true, listening: true, channelId: 'terra', routeEpoch: 2 };
  const result = await router.route({
    botName: 'imperial_guard',
    userId: 'u1',
    text: 'late text',
    lockedTmuxPane: '%42',
    routeEpoch: 1,
    channelId: 'cadia',
  });

  assert.deepEqual(result, { routed: false, ignored: true, reason: 'stale_transcript', cleared: 1 });
  assert.equal(router.listDrafts().length, 0);
  assert.ok(warnings.some(msg => String(msg).includes('ignored stale transcript')));
  assert.deepEqual(ops.slice(-3), [
    ['title', '%42', 'old-title'],
    ['option', '%42', '@DISCORD_VOICE_PROCESSING', '0'],
    ['option', '%42', '@DISCORD_VOICE_LOCK', '0'],
  ]);
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
