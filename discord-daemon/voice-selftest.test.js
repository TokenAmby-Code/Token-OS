// voice-selftest.test.js — pins the voice probe stage machine (PR B).
//
// Everything is DI'd: fake voice manager, fake tmuxctld client, FakeWebSocket
// (per realtime-transcriber.test.js), fake fetch. Never touches live tmux,
// Discord, or OpenAI. The probe must NEVER logger.error (that pages the FG
// fixer hook) — every test asserts zero error-level logs.

import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createVoiceSelftest, SELFTEST_PHRASE, matchTranscript } from './voice-selftest.ts';

class FakeWebSocket extends EventEmitter {
  static instances = [];
  static behavior = 'ack'; // 'ack' | 'silent'

  constructor(url, options) {
    super();
    this.url = url;
    this.options = options;
    this.sent = [];
    this.closed = false;
    FakeWebSocket.instances.push(this);
    if (FakeWebSocket.behavior === 'ack') {
      queueMicrotask(() => this.emit('open'));
    }
  }

  send(payload) {
    this.sent.push(JSON.parse(payload));
    if (FakeWebSocket.behavior === 'ack') {
      queueMicrotask(() => this.emit('message', Buffer.from(JSON.stringify({ type: 'session.updated' }))));
    }
  }

  close() { this.closed = true; }
  terminate() { this.closed = true; }
}

function makeLogger(logs = []) {
  return {
    logs,
    debug(msg) { logs.push(['debug', String(msg)]); },
    info(msg) { logs.push(['info', String(msg)]); },
    warn(msg) { logs.push(['warn', String(msg)]); },
    error(msg) { logs.push(['error', String(msg)]); },
  };
}

function makeFakes({ playAudioImpl = null } = {}) {
  const calls = [];
  const voiceManager = {
    calls,
    async joinChannel(channelId, bot) { calls.push(['join', bot, channelId]); return { channelId, botName: bot }; },
    async leaveChannel(bot, reason) { calls.push(['leave', bot, reason]); return { left: true, botName: bot }; },
    startListening(bot) { calls.push(['listen', bot]); return { listening: true }; },
    async playAudio(file, bot) {
      calls.push(['play', bot, file]);
      if (playAudioImpl) return playAudioImpl(file, bot);
      return { played: true };
    },
    allowProbeSpeaker(probeId, userId, ttlMs) { calls.push(['grant', probeId, userId, ttlMs]); return { granted: true }; },
    revokeProbeSpeaker(probeId) { calls.push(['revoke', probeId]); return true; },
  };
  const transcriber = {
    dropped: [],
    dropBot(bot) { transcriber.dropped.push(bot); return 1; },
  };
  const tmuxctldCalls = [];
  const tmuxctld = {
    calls: tmuxctldCalls,
    async health() { tmuxctldCalls.push(['health']); return { status: 'ok' }; },
    async voiceTarget(bot) { tmuxctldCalls.push(['voiceTarget', bot]); return { target_role: 'mechanicus' }; },
    async startVoiceSession(args) { tmuxctldCalls.push(['start', args]); return { voice_session_id: 'vs-probe' }; },
    async clearVoiceSession(args) { tmuxctldCalls.push(['clear', args]); return { cleared: 1 }; },
    async appendVoiceSession(args) { tmuxctldCalls.push(['append', args]); return {}; },
    async shipVoiceSession(args) { tmuxctldCalls.push(['ship', args]); return {}; },
  };
  const alerts = [];
  const botClients = {
    mechanicus: {
      botUserId: 'mech-user',
      getStatus: () => ({ connected: true }),
      async sendMessage(channelId, content) { alerts.push({ channelId, content }); return { message_id: 'a1' }; },
      client: {},
    },
    inquisition: {
      botUserId: 'inq-user',
      getStatus: () => ({ connected: true }),
      client: {},
    },
  };
  const fetchCalls = [];
  const fetchImpl = async (url, opts) => {
    fetchCalls.push({ url, body: JSON.parse(opts.body) });
    return { ok: true, status: 200, json: async () => ({}) };
  };
  const config = {
    guild_id: 'guild',
    operator_user_id: 'op',
    token_api_port: 7777,
    channels: { alerts: 'alerts-ch' },
    voice_channels: { mechanicus: 'vc-m', custodes: 'vc-c' },
    selftest: { voice_channel_id: 'warp' },
    openai_api_key: 'sk-test',
  };
  return { voiceManager, transcriber, tmuxctld, botClients, fetchCalls, fetchImpl, alerts, config };
}

function makeSelftest(fakes, logs, overrides = {}) {
  return createVoiceSelftest({
    config: fakes.config,
    logger: makeLogger(logs),
    voiceManager: fakes.voiceManager,
    transcriber: fakes.transcriber,
    tmuxctld: fakes.tmuxctld,
    botClients: fakes.botClients,
    fetchImpl: fakes.fetchImpl,
    WebSocketImpl: FakeWebSocket,
    fixturePath: 'fixtures/selftest-phrase.wav',
    fixtureExists: () => true,
    fetchOperatorVoiceChannelId: async () => null,
    watchOperatorVoice: () => () => {},
    openaiTimeoutMs: 60,
    transcriptTimeoutMs: 80,
    ...overrides,
  });
}

function assertNoErrorLogs(logs) {
  assert.deepEqual(logs.filter(([lvl]) => lvl === 'error'), [], 'probe must never logger.error');
}

test('matchTranscript: fuzzy ≥4/7 tokens, punctuation/case insensitive', () => {
  assert.equal(matchTranscript(SELFTEST_PHRASE).matched, true);
  assert.equal(matchTranscript('Golden signal PROBE, verifying!').matched, true); // 4 tokens
  assert.equal(matchTranscript('golden signal probe').matched, false); // 3 tokens
  assert.equal(matchTranscript('completely unrelated words here').matched, false);
});

test('seams probe passes with healthy fakes and records per-stage ms', async () => {
  FakeWebSocket.instances = [];
  FakeWebSocket.behavior = 'ack';
  const fakes = makeFakes();
  const logs = [];
  const selftest = makeSelftest(fakes, logs);

  const report = await selftest.run({ variant: 'seams', trigger: 'boot' });

  assert.equal(report.overall, 'pass');
  assert.equal(report.contract_version, 'voice-selftest.v1');
  assert.deepEqual(report.stages.map(s => s.stage), ['config', 'gateway', 'tmuxctld_health', 'openai_ws']);
  assert.ok(report.stages.every(s => s.ok && typeof s.ms === 'number' && s.ms >= 0));
  assert.equal(FakeWebSocket.instances.length, 1);
  assert.equal(FakeWebSocket.instances[0].closed, true, 'handshake socket closed after session.updated');
  // Events row always; no alert on pass.
  assert.equal(fakes.fetchCalls.length, 1);
  assert.match(fakes.fetchCalls[0].url, /\/api\/events\/log$/);
  assert.equal(fakes.fetchCalls[0].body.event_type, 'voice_selftest');
  assert.deepEqual(fakes.alerts, []);
  assert.equal(selftest.last(), report);
  assertNoErrorLogs(logs);
});

test('openai_ws timeout fails the seams probe and alerts once (deduped)', async () => {
  FakeWebSocket.instances = [];
  FakeWebSocket.behavior = 'silent';
  const fakes = makeFakes();
  const logs = [];
  const selftest = makeSelftest(fakes, logs, { openaiTimeoutMs: 30 });

  const report = await selftest.run({ variant: 'seams', trigger: 'manual' });
  assert.equal(report.overall, 'fail');
  assert.equal(report.first_failed_stage, 'openai_ws');
  const wsStage = report.stages.find(s => s.stage === 'openai_ws');
  assert.equal(wsStage.errorCode, 'openai_ws_timeout');
  assert.equal(fakes.alerts.length, 1);
  assert.match(fakes.alerts[0].content, /FAIL/);

  // Same failure again within the dedupe window: no second alert, lock released.
  const second = await selftest.run({ variant: 'seams', trigger: 'manual' });
  assert.equal(second.overall, 'fail');
  assert.equal(fakes.alerts.length, 1, 'alert deduped by overall+firstFailedStage');
  assertNoErrorLogs(logs);
});

test('tmuxctld timeout degrades the seams probe instead of failing it', async () => {
  FakeWebSocket.instances = [];
  FakeWebSocket.behavior = 'ack';
  const fakes = makeFakes();
  fakes.tmuxctld.health = async () => {
    const err = new Error('tmuxctld timeout /health');
    err.code = 'ETIMEDOUT';
    throw err;
  };
  const logs = [];
  const selftest = makeSelftest(fakes, logs);

  const report = await selftest.run({ variant: 'seams' });
  assert.equal(report.overall, 'degraded');
  const stage = report.stages.find(s => s.stage === 'tmuxctld_health');
  assert.equal(stage.ok, false);
  assert.equal(stage.errorCode, 'tmuxctld_timeout');
  assert.ok(report.stages.find(s => s.stage === 'openai_ws').ok, 'later stages still run');
  assert.equal(fakes.alerts.length, 1, 'degraded alerts');
  assertNoErrorLogs(logs);
});

test('full probe passes end-to-end: grant, join order, transcript match, session start+clear, cleanup', async () => {
  const fakes = makeFakes();
  const logs = [];
  let selftest;
  fakes.voiceManager.playAudio = async (file, bot) => {
    fakes.voiceManager.calls.push(['play', bot, file]);
    setTimeout(() => {
      const consumed = selftest.consumeTranscript({
        botName: 'mechanicus',
        userId: 'inq-user',
        text: SELFTEST_PHRASE,
      });
      assert.equal(consumed, true, 'probe transcript is swallowed (never reaches the router)');
    }, 5);
    return { played: true };
  };
  selftest = makeSelftest(fakes, logs);

  const report = await selftest.run({ variant: 'full', trigger: 'cron' });

  assert.equal(report.overall, 'pass');
  assert.deepEqual(
    report.stages.map(s => s.stage),
    ['operator_gate', 'voice_join', 'audio_loop', 'tmuxctld_session', 'cleanup'],
  );
  assert.deepEqual(report.transcript_match && {
    matched: report.transcript_match.matched,
    attempts: report.transcript_match.attempts,
    passed_on_retry: report.transcript_match.passed_on_retry,
  }, { matched: true, attempts: 1, passed_on_retry: false });

  const calls = fakes.voiceManager.calls;
  const grant = calls.find(c => c[0] === 'grant');
  assert.ok(grant, 'probe speaker grant issued');
  assert.equal(grant[2], 'inq-user');
  assert.ok(calls.findIndex(c => c[0] === 'listen') < calls.findIndex(c => c[0] === 'join' && c[1] === 'inquisition'),
    'listener listening before speaker joins');
  assert.ok(calls.some(c => c[0] === 'revoke'), 'grant revoked in cleanup');
  assert.ok(calls.some(c => c[0] === 'leave' && c[1] === 'inquisition'));
  assert.ok(calls.some(c => c[0] === 'leave' && c[1] === 'mechanicus'));
  assert.deepEqual(fakes.transcriber.dropped, ['mechanicus']);

  const tmux = fakes.tmuxctld.calls.map(c => c[0]);
  assert.ok(tmux.includes('start') && tmux.includes('clear'));
  assert.ok(!tmux.includes('append') && !tmux.includes('ship'), 'probe never appends or ships a draft');
  assert.deepEqual(fakes.alerts, [], 'no alert on pass');
  assertNoErrorLogs(logs);
});

test('wrong phrase fails the audio loop with transcript_mismatch', async () => {
  const fakes = makeFakes();
  const logs = [];
  let selftest;
  fakes.voiceManager.playAudio = async () => {
    setTimeout(() => {
      selftest.consumeTranscript({ botName: 'mechanicus', userId: 'inq-user', text: 'completely unrelated words entirely' });
    }, 5);
    return { played: true };
  };
  selftest = makeSelftest(fakes, logs);

  const report = await selftest.run({ variant: 'full' });
  assert.equal(report.overall, 'fail');
  assert.deepEqual(
    report.stages.map(s => s.stage),
    ['operator_gate', 'voice_join', 'audio_loop', 'cleanup'],
    'audio failure skips tmuxctld_session but still cleans up',
  );
  const stage = report.stages.find(s => s.stage === 'audio_loop');
  assert.equal(stage.errorCode, 'transcript_mismatch');
  assert.equal(report.transcript_match.matched, false);
  assert.ok(
    !fakes.tmuxctld.calls.some(c => c[0] === 'start'),
    'no tmuxctld session is started after the audio loop fails',
  );
  assertNoErrorLogs(logs);
});

test('retry on first-attempt timeout: pass-on-retry marks the run degraded', async () => {
  const fakes = makeFakes();
  const logs = [];
  let selftest;
  let plays = 0;
  fakes.voiceManager.playAudio = async () => {
    plays += 1;
    if (plays === 2) {
      setTimeout(() => {
        selftest.consumeTranscript({ botName: 'mechanicus', userId: 'inq-user', text: SELFTEST_PHRASE });
      }, 5);
    }
    return { played: true };
  };
  selftest = makeSelftest(fakes, logs, { transcriptTimeoutMs: 40 });

  const report = await selftest.run({ variant: 'full' });
  assert.equal(plays, 2);
  assert.equal(report.overall, 'degraded');
  assert.deepEqual(
    { attempts: report.transcript_match.attempts, passed_on_retry: report.transcript_match.passed_on_retry },
    { attempts: 2, passed_on_retry: true },
  );
  assert.ok(report.stages.find(s => s.stage === 'audio_loop').ok);
  assert.equal(fakes.alerts.length, 1, 'degraded still alerts');
  assertNoErrorLogs(logs);
});

test('operator joining mid-probe aborts silently with full cleanup', async () => {
  const fakes = makeFakes();
  const logs = [];
  let operatorJoin = null;
  fakes.voiceManager.playAudio = async () => {
    setTimeout(() => operatorJoin('vc-m'), 5);
    return { played: true };
  };
  const selftest = makeSelftest(fakes, logs, {
    watchOperatorVoice: (onJoin) => { operatorJoin = onJoin; return () => { operatorJoin = null; }; },
  });

  const report = await selftest.run({ variant: 'full' });
  assert.equal(report.overall, 'aborted');
  assert.equal(report.abort_reason, 'operator_active');
  assert.ok(fakes.voiceManager.calls.some(c => c[0] === 'revoke'), 'grant revoked on abort');
  assert.ok(fakes.voiceManager.calls.some(c => c[0] === 'leave' && c[1] === 'inquisition'), 'speaker leaves on abort');
  assert.equal(operatorJoin, null, 'operator watcher unregistered by cleanup');
  assert.deepEqual(fakes.alerts, [], 'operator-caused aborts never alert');
  assert.equal(fakes.fetchCalls.length, 1, 'events row still logged');
  assertNoErrorLogs(logs);
});

test('operator already in a VC gates the probe before any join', async () => {
  const fakes = makeFakes();
  const logs = [];
  const selftest = makeSelftest(fakes, logs, {
    fetchOperatorVoiceChannelId: async () => 'vc-somewhere',
  });

  const report = await selftest.run({ variant: 'full' });
  assert.equal(report.overall, 'aborted');
  assert.equal(report.abort_reason, 'operator_active');
  assert.ok(!fakes.voiceManager.calls.some(c => c[0] === 'join'), 'no VC join when operator is active');
  assert.deepEqual(fakes.alerts, []);
  assertNoErrorLogs(logs);
});

test('double-run returns probe_in_progress while the first probe is live', async () => {
  const fakes = makeFakes();
  const logs = [];
  let releasePlay;
  let selftest;
  fakes.voiceManager.playAudio = () => new Promise((resolve) => {
    releasePlay = () => {
      setTimeout(() => {
        selftest.consumeTranscript({ botName: 'mechanicus', userId: 'inq-user', text: SELFTEST_PHRASE });
      }, 5);
      resolve({ played: true });
    };
  });
  selftest = makeSelftest(fakes, logs);

  const first = selftest.run({ variant: 'full' });
  await new Promise(r => setTimeout(r, 10));
  const second = await selftest.run({ variant: 'seams' });
  assert.equal(second.errorCode, 'probe_in_progress');

  releasePlay();
  const report = await first;
  assert.equal(report.overall, 'pass');

  // Lock released: a new probe can run now.
  FakeWebSocket.behavior = 'ack';
  const third = await selftest.run({ variant: 'seams' });
  assert.notEqual(third.errorCode, 'probe_in_progress');
  assertNoErrorLogs(logs);
});

test('cleanup runs even when a stage throws', async () => {
  const fakes = makeFakes();
  const logs = [];
  fakes.voiceManager.joinChannel = async () => { throw new Error('join exploded'); };
  const selftest = makeSelftest(fakes, logs);

  const report = await selftest.run({ variant: 'full' });
  assert.equal(report.overall, 'fail');
  assert.equal(report.first_failed_stage, 'voice_join');
  const cleanup = report.stages.find(s => s.stage === 'cleanup');
  assert.ok(cleanup, 'cleanup stage recorded');
  assert.ok(fakes.voiceManager.calls.some(c => c[0] === 'revoke'), 'grant revoked despite stage throw');
  assertNoErrorLogs(logs);
});

test('hard deadline aborts a wedged probe, finalizes the report, and alerts', async () => {
  const fakes = makeFakes();
  const logs = [];
  fakes.voiceManager.playAudio = () => new Promise(() => {}); // wedged forever
  const selftest = makeSelftest(fakes, logs, { fullDeadlineMs: 50, transcriptTimeoutMs: 30_000, abortGraceMs: 30 });

  const report = await selftest.run({ variant: 'full' });
  assert.equal(report.overall, 'aborted');
  assert.equal(report.abort_reason, 'deadline');
  assert.equal(fakes.alerts.length, 1, 'deadline aborts alert (unlike operator aborts)');

  // Lock is released even though the wedged stage never resolved.
  FakeWebSocket.instances = [];
  FakeWebSocket.behavior = 'ack';
  const next = await selftest.run({ variant: 'seams' });
  assert.notEqual(next.errorCode, 'probe_in_progress');
  assertNoErrorLogs(logs);
});
