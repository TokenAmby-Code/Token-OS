// voice.playchain.test.js — pins the per-bot playback serialization (PR A).
//
// Decree: "one route, one authority, one serialized queue — no two voices ever
// overlap." The server-side queue is the primary guarantee; `playChain` is the
// daemon-level defense-in-depth: even if two callers reach `playAudio` on one bot
// concurrently, the second must await the first via the promise-chain mutex and
// never invoke `player.play()` mid-line.
//
// This lives in its own file (not voice.test.js) because it module-mocks
// '@discordjs/voice' and must import voice.ts FRESH, after the mock is installed —
// voice.test.js statically imports voice.ts, which would bind the real deps first.
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
    // Real discord.js takes the player to Idle on stop(); mirror that so the
    // in-flight playAudioNow promise resolves.
    stop() {
      this.emit('idle');
    },
  };
}

function fakeDiscordVoiceExports(player, connection) {
  return {
    namedExports: {
      joinVoiceChannel: () => connection,
      entersState: async () => true,
      VoiceConnectionStatus: { Ready: 'ready' },
      AudioPlayerStatus: { Playing: 'playing', Idle: 'idle' },
      StreamType: { Raw: 'raw' },
      EndBehaviorType: { AfterSilence: 1 },
      createAudioPlayer: () => player,
      createAudioResource: () => ({}),
    },
  };
}

function fakeBotClient() {
  return {
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
}

const tick = () => new Promise((r) => setTimeout(r, 10));

function makeReadyConnection() {
  return {
    state: { status: 'ready' },
    subscribe() {},
    destroy() {},
    receiver: { speaking: { on() {} } },
  };
}

// Each test imports voice.ts under a unique query so it re-evaluates against its
// own active mock — ESM caches modules by resolved URL, so two tests sharing
// './voice.ts' would otherwise both bind the first test's fake player.
async function loadVoiceManager(player, tag) {
  mock.module('@discordjs/voice', fakeDiscordVoiceExports(player, makeReadyConnection()));
  const { createVoiceManager } = await import(`./voice.ts?case=${tag}`);
  const vm = createVoiceManager(
    { mechanicus: fakeBotClient() },
    { guild_id: 'g', operator_user_id: 'op', voice_channels: { mechanicus: 'vc' } },
    { debug() {}, info() {}, warn() {}, error() {} },
  );
  await vm.joinChannel('vc', 'mechanicus');
  return vm;
}

function tmpPcm(name) {
  const path = join(tmpdir(), name);
  writeFileSync(path, 'x'); // playAudioNow requires the file to exist before play()
  return path;
}

test('two concurrent playAudio on one bot serialize via playChain (no overlap)', async () => {
  const player = makeFakePlayer();
  const vm = await loadVoiceManager(player, 'serialize');
  const fileA = tmpPcm('pra-playchain-a.pcm');
  const fileB = tmpPcm('pra-playchain-b.pcm');

  // Fire both plays at once. Neither auto-finishes; the fake player only goes Idle
  // when we emit it, so we can observe whether the second started early.
  const first = vm.playAudio(fileA, 'mechanicus');
  const second = vm.playAudio(fileB, 'mechanicus');

  await tick();
  // Only the first line may have reached player.play(); the second is parked.
  assert.equal(player.playCalls.length, 1, 'second play must wait for the first');

  player.emit('idle'); // first line finishes
  await first;
  await tick();

  // Now — and only now — the second line starts.
  assert.equal(player.playCalls.length, 2, 'second play runs after the first completes');
  player.emit('idle');
  await second;

  mock.reset();
});

test('stopPlayback drains the queued backlog instead of resuming after Idle', async () => {
  const player = makeFakePlayer();
  const vm = await loadVoiceManager(player, 'stop');
  const fileA = tmpPcm('pra-playchain-stop-a.pcm');
  const fileB = tmpPcm('pra-playchain-stop-b.pcm');

  const first = vm.playAudio(fileA, 'mechanicus');
  const second = vm.playAudio(fileB, 'mechanicus');

  await tick();
  assert.equal(player.playCalls.length, 1, 'first line is playing, second queued');

  // Stop while a backlog exists: it invalidates the queued generation AND takes the
  // current line to Idle. The queued second line must be dropped, not played next.
  vm.stopPlayback('mechanicus');
  await first;

  const secondResult = await second;
  await tick();

  assert.equal(player.playCalls.length, 1, 'queued line must NOT play after stop');
  assert.equal(secondResult.skipped, true, 'queued line is dropped with skipped=true');

  mock.reset();
});
