// voice.decoder-recovery.test.js — pins bounded in-place decoder recovery (PR A).
//
// The sporadic "compressed data passed is corrupted" opus decoder death used to
// delete the audio subscription permanently: one corrupted frame cost the rest
// of the utterance and every later one until a VC rejoin. Now the decoder is
// recreated in place (bounded per subscription) with all per-utterance closure
// state intact; only exhaustion tears the subscription down.
//
// Own file because it module-mocks '@discordjs/voice', 'prism-media', and
// './tmuxctld-client.ts' and must import voice.ts FRESH after the mocks are
// installed (see voice.playchain.test.js for the pattern). Never touches live
// tmux — the tmuxctld client is fully faked.

import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough } from 'stream';

const tick = () => new Promise((r) => setTimeout(r, 10));

function makeFakes() {
  const decoders = [];
  class FakeDecoder extends PassThrough {
    constructor() {
      super();
      this.decoded = [];
      this.on('data', (chunk) => this.decoded.push(chunk));
      decoders.push(this);
    }
  }

  let speakingStart = null;
  const audioStream = new PassThrough();
  const connection = {
    state: { status: 'ready' },
    subscribe() {},
    destroy() {},
    receiver: {
      speaking: {
        on(event, cb) {
          if (event === 'start') speakingStart = cb;
        },
      },
      subscribe: () => audioStream,
    },
  };

  const tmuxctldCalls = [];
  const fakeTmuxctldClient = {
    startVoiceSession(args) {
      tmuxctldCalls.push(['start', args]);
      return Promise.resolve({ voice_session_id: 'vs-test' });
    },
    clearVoiceSession(args) {
      tmuxctldCalls.push(['clear', args]);
      return Promise.resolve({ cleared: 0 });
    },
    sendText() { return Promise.resolve({}); },
    voiceTarget() { return Promise.resolve({ target_role: 'mechanicus' }); },
    health() { return Promise.resolve({ ok: true }); },
  };

  return {
    decoders,
    FakeDecoder,
    audioStream,
    connection,
    tmuxctldCalls,
    fakeTmuxctldClient,
    getSpeakingStart: () => speakingStart,
  };
}

async function loadListeningManager(tag) {
  const fakes = makeFakes();
  mock.module('@discordjs/voice', {
    namedExports: {
      joinVoiceChannel: () => fakes.connection,
      entersState: async () => true,
      VoiceConnectionStatus: { Ready: 'ready' },
      AudioPlayerStatus: { Playing: 'playing', Idle: 'idle' },
      StreamType: { Raw: 'raw' },
      EndBehaviorType: { Manual: 0, AfterSilence: 1 },
      createAudioPlayer: () => ({ on() {}, removeListener() {}, play() {}, stop() {} }),
      createAudioResource: () => ({}),
    },
  });
  mock.module('prism-media', {
    defaultExport: { opus: { Decoder: fakes.FakeDecoder } },
  });
  mock.module('./tmuxctld-client.ts', {
    namedExports: {
      tmuxctldClient: fakes.fakeTmuxctldClient,
      createTmuxctldClient: () => fakes.fakeTmuxctldClient,
    },
  });

  const logs = [];
  const logger = {
    debug(msg, meta) { logs.push(['debug', String(msg), meta]); },
    info(msg, meta) { logs.push(['info', String(msg), meta]); },
    warn(msg, meta) { logs.push(['warn', String(msg), meta]); },
    error(msg, meta) { logs.push(['error', String(msg), meta]); },
  };

  const { createVoiceManager } = await import(`./voice.ts?case=${tag}`);
  const vm = createVoiceManager(
    {
      mechanicus: {
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
      },
    },
    { guild_id: 'g', operator_user_id: 'op', voice_channels: { mechanicus: 'vc' } },
    logger,
  );

  const audioEnds = [];
  vm.setAudioEndCallback((userId, botName) => audioEnds.push({ userId, botName }));

  await vm.joinChannel('vc', 'mechanicus');
  vm.startListening('mechanicus');
  fakes.getSpeakingStart()('op');
  await tick();

  return { vm, fakes, logs, audioEnds };
}

const speechFrame = () => Buffer.alloc(60, 0xab);

test('corrupted-frame decoder error recreates decoder in place, keeps the subscription', async () => {
  const { vm, fakes, logs, audioEnds } = await loadListeningManager('recover');

  assert.equal(fakes.decoders.length, 1, 'one decoder after subscribe');
  fakes.audioStream.write(speechFrame());
  await tick();
  assert.equal(fakes.decoders[0].decoded.length, 1, 'audio flows through the first decoder');

  // Inject a decode failure like the ~monthly "compressed data passed is corrupted".
  fakes.decoders[0].destroy(new Error('decoder error compressed data passed is corrupted'));
  await tick();

  assert.equal(fakes.decoders.length, 2, 'decoder recreated in place');
  assert.equal(vm.getStatus('mechanicus').activeListeners, 1, 'subscription survives the decode failure');
  assert.deepEqual(audioEnds, [], 'no audio-end teardown on a recovered decode failure');

  const recovery = logs.find(([lvl, msg]) => lvl === 'warn' && msg.includes('recreating decoder'));
  assert.ok(recovery, 'recovery logged at warn (no fixer page)');
  assert.equal(recovery[2].errorCode, 'opus_decode_failed');
  assert.match(recovery[1], /frame_len=60/, 'diagnostics carry the last forwarded frame length');
  assert.ok(!logs.some(([lvl]) => lvl === 'error'), 'no error-level log for a recovered failure');

  // The utterance continues into the fresh decoder.
  fakes.audioStream.write(speechFrame());
  await tick();
  assert.equal(fakes.decoders[1].decoded.length, 1, 'audio flows through the recreated decoder');

  mock.reset();
});

test('decoder recovery is bounded: exhaustion tears down and emits a typed error', async () => {
  const { vm, fakes, logs, audioEnds } = await loadListeningManager('exhaust');

  fakes.audioStream.write(speechFrame());
  await tick();

  // 3 recoveries allowed per subscription; the 4th decode failure is terminal.
  for (let i = 0; i < 4; i++) {
    fakes.decoders[fakes.decoders.length - 1].destroy(new Error('corrupted'));
    await tick();
  }

  assert.equal(fakes.decoders.length, 4, 'three recreations, then no more');
  assert.equal(vm.getStatus('mechanicus').activeListeners, 0, 'subscription torn down after exhaustion');
  assert.equal(audioEnds.length, 1, 'audio-end fires exactly once on terminal teardown');

  const terminal = logs.find(([lvl, msg]) => lvl === 'error' && msg.includes('recoveries exhausted'));
  assert.ok(terminal, 'exhaustion logged at error (pages the fixer)');
  assert.equal(terminal[2].errorCode, 'opus_decode_failed');

  mock.reset();
});
