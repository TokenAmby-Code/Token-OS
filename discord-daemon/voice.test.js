// voice.test.js — pins Discord-owned voice lifecycle behavior.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { createVoiceManager } from './voice.js';

test('voice manager source does not resolve or store raw pane locks', () => {
  const source = readFileSync(new URL('./voice.js', import.meta.url), 'utf8');
  assert.equal(source.includes('lockedTmuxPane'), false);
  assert.equal(source.includes('execFileSync'), false);
  assert.equal(source.includes("'tmux'"), false);
  assert.equal(source.includes('resolveSelectedTmuxPane'), false);
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
