// voice.test.js — pins tmux field parsing used by launchd-hosted voice routing.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createVoiceManager, parseTmuxFields, TMUX_FIELD_SEP } from './voice.js';

test('tmux field parsing does not depend on literal tab output', () => {
  const line = ['1780603214', '/dev/ttys024', 'main'].join(TMUX_FIELD_SEP);

  assert.deepEqual(parseTmuxFields(line), ['1780603214', '/dev/ttys024', 'main']);
});

test('tmux field parsing preserves literal tabs inside field values', () => {
  const sessionName = 'main\twith-tab';
  const line = ['1780603214', '/dev/ttys024', sessionName].join(TMUX_FIELD_SEP);

  assert.deepEqual(parseTmuxFields(line), ['1780603214', '/dev/ttys024', sessionName]);
});

test('tmux field parsing handles the four-field paneInfo format', () => {
  const fields = ['%42', 'main', 'nvim', '/Users/tokenclaw/project'];
  const line = fields.join(TMUX_FIELD_SEP);

  assert.deepEqual(parseTmuxFields(line), fields);
});

test('tmux field separator is explicit for launchd C-locale tmux calls', () => {
  assert.equal(TMUX_FIELD_SEP.includes('\t'), false);
  // Keep the separator visually distinct from path fragments such as pane_current_path values.
  assert.equal(TMUX_FIELD_SEP.includes('_/'), false);
});

test('VC hop runs old-channel leave cleanup before new-channel join routing', async () => {
  const logs = [];
  let voiceStateUpdate = null;
  const eventClient = {
    on(event, cb) {
      if (event === 'voiceStateUpdate') voiceStateUpdate = cb;
    },
  };
  const logger = {
    debug(msg) { logs.push(['debug', msg]); },
    info(msg) { logs.push(['info', msg]); },
    warn(msg) { logs.push(['warn', msg]); },
    error(msg) { logs.push(['error', msg]); },
  };

  const voiceManager = createVoiceManager(
    {
      custodes: { client: eventClient },
      imperial_guard: { client: eventClient },
    },
    {
      guild_id: 'guild',
      operator_user_id: 'operator',
      voice_channels: {
        custodes: 'terra',
        imperial_guard: 'cadia',
      },
    },
    logger,
  );

  const leaveEvents = [];
  voiceManager.setVoiceLeaveCallback(async (botName, meta) => {
    leaveEvents.push({ botName, reason: meta.reason, channelId: meta.channelId });
  });
  voiceManager.setupAutoJoin();

  assert.equal(typeof voiceStateUpdate, 'function');

  await voiceStateUpdate(
    { channelId: 'cadia', member: { id: 'operator' } },
    { channelId: 'terra', member: { id: 'operator' } },
  );

  assert.deepEqual(leaveEvents, [
    {
      botName: 'imperial_guard',
      reason: 'explicit-vc-hop cadia->terra',
      channelId: 'cadia',
    },
  ]);
  const cleanupIndex = logs.findIndex(([, msg]) => String(msg).includes('explicit-vc-hop cadia->terra'));
  const joinIndex = logs.findIndex(([, msg]) => String(msg).includes('operator joined terra'));
  assert.notEqual(cleanupIndex, -1);
  assert.notEqual(joinIndex, -1);
  assert.ok(cleanupIndex < joinIndex);
});
