// voice.playchain.test.js — pins the per-bot playback serialization (PR A).
//
// Decree: "one route, one authority, one serialized queue — no two voices ever
// overlap." The server-side queue is the primary guarantee; `playChain` is the
// daemon-level defense-in-depth: even if two callers reach `playAudio` on one bot
// concurrently, the second must await the first via the promise-chain mutex and
// never invoke `player.play()` mid-line.
//
// This lives in its own file (not voice.test.js) because it module-mocks
// '@discordjs/voice' and must import voice.js FRESH, after the mock is installed —
// voice.test.js statically imports voice.js, which would bind the real deps first.
// Requires the runner flag `--experimental-test-module-mocks` (set in package.json).

import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import { writeFileSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';

function makeFakePlayer() {
  const listeners = {};
  return {
    playCalls: [],
    on(event, cb) {
      (listeners[event] ||= []).push(cb);
    },
    removeListener(event, cb) {
      listeners[event] = (listeners[event] || []).filter((f) => f !== cb);
    },
    emit(event, ...args) {
      (listeners[event] || []).slice().forEach((f) => f(...args));
    },
    play(resource) {
      this.playCalls.push(resource);
    },
  };
}

test('two concurrent playAudio on one bot serialize via playChain (no overlap)', async () => {
  const player = makeFakePlayer();
  const fakeConnection = {
    state: { status: 'ready' },
    subscribe() {},
    destroy() {},
    receiver: { speaking: { on() {} } },
  };

  mock.module('@discordjs/voice', {
    namedExports: {
      joinVoiceChannel: () => fakeConnection,
      entersState: async () => true,
      VoiceConnectionStatus: { Ready: 'ready' },
      AudioPlayerStatus: { Playing: 'playing', Idle: 'idle' },
      StreamType: { Raw: 'raw' },
      EndBehaviorType: { AfterSilence: 1 },
      createAudioPlayer: () => player,
      createAudioResource: () => ({}),
    },
  });

  const { createVoiceManager } = await import('./voice.js');

  // playAudioNow requires the file to exist before it reaches player.play().
  const fileA = join(tmpdir(), 'pra-playchain-a.pcm');
  const fileB = join(tmpdir(), 'pra-playchain-b.pcm');
  writeFileSync(fileA, 'x');
  writeFileSync(fileB, 'x');

  const botClient = {
    client: {
      user: { id: 'bot' },
      guilds: {
        fetch: async () => ({
          channels: {
            fetch: async () => ({ isVoiceBased: () => true, name: 'vc' }),
          },
        }),
      },
    },
  };
  const voiceManager = createVoiceManager(
    { mechanicus: botClient },
    { guild_id: 'g', operator_user_id: 'op', voice_channels: { mechanicus: 'vc' } },
    { debug() {}, info() {}, warn() {}, error() {} },
  );

  await voiceManager.joinChannel('vc', 'mechanicus');

  // Fire both plays at once. Neither auto-finishes; the fake player only goes Idle
  // when we emit it, so we can observe whether the second started early.
  const first = voiceManager.playAudio(fileA, 'mechanicus');
  const second = voiceManager.playAudio(fileB, 'mechanicus');

  await new Promise((r) => setTimeout(r, 10));
  // Only the first line may have reached player.play(); the second is parked.
  assert.equal(player.playCalls.length, 1, 'second play must wait for the first');

  player.emit('idle'); // first line finishes
  await first;
  await new Promise((r) => setTimeout(r, 10));

  // Now — and only now — the second line starts.
  assert.equal(player.playCalls.length, 2, 'second play runs after the first completes');
  player.emit('idle');
  await second;

  mock.reset();
});
