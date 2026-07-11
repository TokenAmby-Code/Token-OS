// voice-selftest.ts — end-to-end probes for the Discord voice pipeline.
//
// Nothing used to exercise the audio path until the operator spoke, so a dead
// decoder, a wedged Realtime handshake, or a missing tmuxctld voice target was
// indistinguishable from "working". Two probe variants:
//
// - seams (≤15s, no audio): config / gateway / tmuxctld / OpenAI Realtime
//   handshake. Fired ~5s after boot, fire-and-forget.
// - full (≤60s): inquisition (no voice_channels assignment — invisible to
//   auto-join routing) speaks a committed fixture phrase into the dedicated
//   probe VC while mechanicus listens; the transcript must fuzzy-match the
//   phrase. tmuxctld session start+clear only — the probe never appends or
//   ships, and probe transcripts never reach the router (consumeTranscript).
//
// Probe rules:
// - The operator always wins: any operator VC presence aborts, including
//   mid-probe via an additive VoiceStateUpdate listener. Operator aborts are
//   surfaced to the events table but never alert.
// - The ouroboros exception is probe-scoped and fail-closed: a TTL'd grant in
//   the voice manager, revoked in cleanup — a crashed probe can never leave
//   bots permanently unfiltered.
// - NEVER logger.error from the probe: error-level logs page the FG fixer
//   hook, and a boot probe would page after every restart. warn + explicit
//   sinks (events row always; alerts channel only on fail/degraded/deadline).

import { existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';
import WebSocket from 'ws';

const __dirname = dirname(fileURLToPath(import.meta.url));

export const SELFTEST_PHRASE = 'golden signal probe verifying discord audio loop';
export const SELFTEST_PHRASE_TOKENS = SELFTEST_PHRASE.split(' ');
export const SELFTEST_MIN_MATCHED_TOKENS = 4;
export const DEFAULT_FIXTURE_PATH = join(__dirname, 'fixtures', 'selftest-phrase.wav');
export const SEAMS_DEADLINE_MS = 15_000;
export const FULL_DEADLINE_MS = 60_000;

const CONTRACT_VERSION = 'voice-selftest.v1';
const REALTIME_URL = 'wss://api.openai.com/v1/realtime?intent=transcription';

export function matchTranscript(text) {
  const words = new Set(
    String(text || '')
      .toLowerCase()
      .replace(/[^a-z0-9\s]+/g, '')
      .split(/\s+/)
      .filter(Boolean),
  );
  const matchedTokens = SELFTEST_PHRASE_TOKENS.filter((t) => words.has(t)).length;
  return {
    matched: matchedTokens >= SELFTEST_MIN_MATCHED_TOKENS,
    matched_tokens: matchedTokens,
    total_tokens: SELFTEST_PHRASE_TOKENS.length,
  };
}

function normalizeBot(botName) {
  return String(botName || '').trim().toLowerCase().replaceAll('-', '_');
}

/**
 * @typedef {object} VoiceSelftest
 * @property {function({variant?: string, trigger?: string}=): Promise<object>} run
 * @property {function(): object|null} last
 * @property {function(object): boolean} consumeTranscript
 */

/**
 * @returns {VoiceSelftest}
 */
export function createVoiceSelftest({
  config,
  logger,
  voiceManager,
  transcriber = null,
  tmuxctld,
  botClients = {},
  fetchImpl = fetch,
  WebSocketImpl = WebSocket,
  fixturePath = DEFAULT_FIXTURE_PATH,
  fixtureExists = (path) => existsSync(path),
  fetchOperatorVoiceChannelId = null,
  watchOperatorVoice = null,
  sendAlert = null,
  speakerBot = 'inquisition',
  listenerBot = 'mechanicus',
  seamsDeadlineMs = SEAMS_DEADLINE_MS,
  fullDeadlineMs = FULL_DEADLINE_MS,
  abortGraceMs = 2_000,
  openaiTimeoutMs = 10_000,
  transcriptTimeoutMs = 20_000,
  playAudioTimeoutMs = 15_000,
  audioLoopAttempts = 2,
  alertDedupeMs = 60 * 60_000,
  probeGrantTtlMs = 90_000,
  daemonVersion = '',
}) {
  let active = null;
  let lastReport = null;
  let probeCounter = 0;
  const alertLastSent = new Map(); // `${overall}:${firstFailedStage}` -> ts

  function eventClient() {
    return Object.values(botClients || {}).find((c) => c?.client) || null;
  }

  const defaultFetchOperatorVoiceChannelId = async () => {
    const holder = eventClient();
    if (!holder?.client) return null;
    const guild = await holder.client.guilds.fetch(config.guild_id);
    const member = await guild.members.fetch(config.operator_user_id);
    return member?.voice?.channelId || null;
  };

  // Additive listener only — never detaches or replaces the voice manager's
  // own auto-join routing listener.
  const defaultWatchOperatorVoice = (onJoin) => {
    const holder = eventClient();
    if (!holder?.client?.on) return () => {};
    const handler = (oldState, newState) => {
      const memberId = newState?.member?.id || oldState?.member?.id;
      if (memberId !== config.operator_user_id) return;
      if (newState?.channelId) onJoin(newState.channelId);
    };
    holder.client.on('voiceStateUpdate', handler);
    return () => {
      try { holder.client.off?.('voiceStateUpdate', handler); } catch {}
    };
  };

  const defaultSendAlert = async (content) => {
    const channelId = config.channels?.alerts;
    if (!channelId) return { skipped: true, reason: 'no_alerts_channel' };
    const holder = botClients?.[listenerBot] || eventClient();
    if (!holder?.sendMessage) return { skipped: true, reason: 'no_client' };
    return holder.sendMessage(channelId, content);
  };

  const getOperatorChannel = fetchOperatorVoiceChannelId || defaultFetchOperatorVoiceChannelId;
  const watchOperator = watchOperatorVoice || defaultWatchOperatorVoice;
  const postAlert = sendAlert || defaultSendAlert;

  function openaiApiKey() {
    return config.openai_api_key || process.env.OPENAI_API_KEY || '';
  }

  function stageError(errorCode, message) {
    const err = new Error(message || errorCode);
    err.errorCode = errorCode;
    return err;
  }

  async function runStage(ctx, stage, severity, fn) {
    if (ctx.aborted) return null;
    const startedAt = Date.now();
    const record = { stage, ok: false, ms: 0, severity };
    try {
      const detail = await fn();
      record.ok = true;
      if (typeof detail === 'string' && detail) record.detail = detail;
    } catch (err) {
      record.errorCode = err?.errorCode || err?.code || 'stage_failed';
      record.detail = err?.message || String(err);
      logger.warn(
        `Voice selftest [${ctx.probeId}]: stage ${stage} failed (${record.errorCode}): ${record.detail}`,
      );
    } finally {
      record.ms = Date.now() - startedAt;
      ctx.stages.push(record);
    }
    return record;
  }

  // --- seams stages ---

  async function stageConfig() {
    const missing = [];
    if (!openaiApiKey()) missing.push('openai_api_key');
    if (!config.guild_id) missing.push('guild_id');
    if (!config.operator_user_id) missing.push('operator_user_id');
    if (!Object.keys(config.voice_channels || {}).length) missing.push('voice_channels');
    if (!fixtureExists(fixturePath)) missing.push('selftest_fixture');
    if (missing.length) throw stageError('missing_config', `missing: ${missing.join(', ')}`);
    return `ok (${Object.keys(config.voice_channels || {}).length} voice bots)`;
  }

  async function stageGateway() {
    const down = [];
    for (const [name, holder] of Object.entries(botClients || {})) {
      const status = holder?.getStatus?.();
      if (!status?.connected) down.push(name);
    }
    if (!Object.keys(botClients || {}).length) throw stageError('no_bot_clients', 'no bot clients');
    if (down.length) throw stageError('gateway_not_ready', `disconnected: ${down.join(', ')}`);
    return `all ${Object.keys(botClients).length} bots READY`;
  }

  async function stageTmuxctldHealth() {
    try {
      await tmuxctld.health({ timeoutMs: 3_000 });
    } catch (err) {
      throw stageError(err?.code === 'ETIMEDOUT' ? 'tmuxctld_timeout' : 'tmuxctld_unreachable', err?.message);
    }
    try {
      const target = await tmuxctld.voiceTarget(listenerBot);
      return `target_role=${target?.target_role || 'unknown'}`;
    } catch (err) {
      throw stageError('no_voice_target', err?.message);
    }
  }

  function stageOpenaiWs() {
    const apiKey = openaiApiKey();
    if (!apiKey) return Promise.reject(stageError('missing_openai_key', 'no OpenAI API key'));
    return new Promise((resolve, reject) => {
      let settled = false;
      const ws = new WebSocketImpl(REALTIME_URL, {
        headers: { Authorization: `Bearer ${apiKey}` },
      });
      const finish = (err) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try { ws.close(); } catch {}
        if (err) reject(err);
        else resolve('session.updated acknowledged');
      };
      const timer = setTimeout(() => {
        try { ws.terminate?.(); } catch {}
        finish(stageError('openai_ws_timeout', `no session.updated within ${openaiTimeoutMs}ms`));
      }, openaiTimeoutMs);
      ws.on('open', () => {
        ws.send(JSON.stringify({
          type: 'session.update',
          session: {
            type: 'transcription',
            audio: {
              input: {
                format: { type: 'audio/pcm', rate: 24000 },
                transcription: { model: config.realtime_transcription_model || 'gpt-4o-transcribe' },
              },
            },
          },
        }));
      });
      ws.on('message', (raw) => {
        let event;
        try { event = JSON.parse(raw.toString()); } catch { return; }
        if (event.type === 'session.updated') finish(null);
        else if (event.type === 'error') {
          finish(stageError('openai_ws_error', event.error?.message || 'realtime error'));
        }
      });
      ws.on('error', (err) => finish(stageError('openai_ws_error', err?.message)));
    });
  }

  // --- full-probe stages ---

  function speakerUserId() {
    const holder = botClients?.[speakerBot];
    return holder?.botUserId || holder?.client?.user?.id || null;
  }

  async function stageOperatorGate(ctx) {
    const channelId = await getOperatorChannel();
    if (channelId) {
      abortProbe(ctx, 'operator_active');
      throw stageError('operator_active', `operator in VC ${channelId}`);
    }
    // The operator always wins, including mid-probe.
    ctx.unwatchOperator = watchOperator(() => abortProbe(ctx, 'operator_active'));
    return 'operator not in voice';
  }

  async function stageVoiceJoin(ctx) {
    const probeChannelId = config.selftest?.voice_channel_id;
    if (!probeChannelId) throw stageError('no_probe_channel', 'selftest.voice_channel_id not configured');
    const speakerId = speakerUserId();
    if (!speakerId) throw stageError('speaker_bot_unavailable', `bot '${speakerBot}' has no user id`);

    // Probe-scoped, TTL'd ouroboros exception — fail-closed by expiry.
    voiceManager.allowProbeSpeaker(ctx.probeId, speakerId, probeGrantTtlMs);
    ctx.grantIssued = true;
    ctx.speakerUserId = String(speakerId);

    // Listener first so it is already subscribed when the speaker's audio starts.
    // Join intent is marked before each await so cleanup covers partial joins.
    ctx.joinedListener = true;
    await voiceManager.joinChannel(probeChannelId, listenerBot);
    voiceManager.startListening(listenerBot);
    ctx.joinedSpeaker = true;
    await voiceManager.joinChannel(probeChannelId, speakerBot);
    return `joined ${probeChannelId} (speaker=${speakerBot}, listener=${listenerBot})`;
  }

  // Bound an external call (Discord join/play) with both the probe abort
  // signal and its own timeout, so a wedged dependency can never hold the
  // runner past cleanup — the stage fails typed and runFull unwinds normally.
  function raceStageCall(ctx, promise, timeoutMs, errorCode, what) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        fn(value);
      };
      const timer = setTimeout(() => {
        finish(reject, stageError(errorCode, `${what} did not complete within ${timeoutMs}ms`));
      }, timeoutMs);
      ctx.abortWaiters.push(() => finish(reject, stageError('aborted', ctx.abortReason || 'aborted')));
      Promise.resolve(promise).then(
        (value) => finish(resolve, value),
        (err) => finish(reject, err),
      );
    });
  }

  function waitForTranscript(ctx) {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        ctx.transcriptSink = null;
        resolve(null);
      }, transcriptTimeoutMs);
      ctx.abortWaiters.push(() => { clearTimeout(timer); resolve(null); });
      ctx.transcriptSink = (text) => {
        clearTimeout(timer);
        ctx.transcriptSink = null;
        resolve(String(text || ''));
      };
    });
  }

  async function stageAudioLoop(ctx) {
    for (let attempt = 1; attempt <= audioLoopAttempts; attempt++) {
      if (ctx.aborted) throw stageError('aborted', ctx.abortReason || 'aborted');
      const wait = waitForTranscript(ctx);
      await raceStageCall(
        ctx,
        voiceManager.playAudio(fixturePath, speakerBot),
        playAudioTimeoutMs,
        'play_timeout',
        `playAudio(${speakerBot})`,
      );
      const text = await wait;
      if (ctx.aborted) throw stageError('aborted', ctx.abortReason || 'aborted');
      if (text === null) {
        logger.warn(`Voice selftest [${ctx.probeId}]: no transcript within ${transcriptTimeoutMs}ms (attempt ${attempt}/${audioLoopAttempts})`);
        continue;
      }
      const match = matchTranscript(text);
      ctx.transcriptMatch = {
        ...match,
        attempts: attempt,
        passed_on_retry: attempt > 1,
        transcript: text,
      };
      if (!match.matched) {
        throw stageError(
          'transcript_mismatch',
          `matched ${match.matched_tokens}/${match.total_tokens} tokens: "${text}"`,
        );
      }
      // Pass-on-retry tolerates the ~monthly single-frame corruption without a
      // false FAIL, but the retry itself is signal — mark the run degraded.
      if (attempt > 1) ctx.degraded = true;
      return `matched ${match.matched_tokens}/${match.total_tokens} tokens (attempt ${attempt})`;
    }
    ctx.transcriptMatch = {
      matched: false,
      matched_tokens: 0,
      total_tokens: SELFTEST_PHRASE_TOKENS.length,
      attempts: audioLoopAttempts,
      passed_on_retry: false,
      transcript: null,
    };
    throw stageError('no_transcript', `no transcript after ${audioLoopAttempts} attempts`);
  }

  async function stageTmuxctldSession(ctx) {
    // start + clear ONLY — the probe must never append into or ship a draft.
    const started = await tmuxctld.startVoiceSession({
      botName: listenerBot,
      userId: `selftest:${ctx.probeId}`,
      channelId: config.selftest?.voice_channel_id || '',
      routeEpoch: 'selftest',
    });
    const sessionId = started?.voice_session_id || '';
    if (!sessionId) throw stageError('no_voice_session', 'start returned no voice_session_id');
    await tmuxctld.clearVoiceSession({ voiceSessionId: sessionId, timeoutMs: 5_000 });
    return `session ${sessionId} started and cleared`;
  }

  async function runCleanup(ctx) {
    // Single cleanup path, each step individually caught: a failed leave must
    // not strand the grant revoke, and vice versa.
    const startedAt = Date.now();
    const steps = [
      ['revoke_grant', () => { if (ctx.grantIssued) voiceManager.revokeProbeSpeaker(ctx.probeId); }],
      ['leave_speaker', () => (ctx.joinedSpeaker ? voiceManager.leaveChannel(speakerBot, 'selftest-cleanup') : null)],
      ['leave_listener', () => (ctx.joinedListener ? voiceManager.leaveChannel(listenerBot, 'selftest-cleanup') : null)],
      ['drop_realtime_sessions', () => transcriber?.dropBot?.(listenerBot)],
      ['unregister_sink', () => {
        ctx.transcriptSink = null;
        for (const release of ctx.abortWaiters.splice(0)) release();
        ctx.unwatchOperator?.();
        ctx.unwatchOperator = null;
      }],
    ];
    const failures = [];
    for (const [name, fn] of steps) {
      try {
        await fn();
      } catch (err) {
        failures.push(name);
        logger.warn(`Voice selftest [${ctx.probeId}]: cleanup step ${name} failed: ${err?.message}`);
      }
    }
    ctx.stages.push({
      stage: 'cleanup',
      ok: failures.length === 0,
      ms: Date.now() - startedAt,
      severity: 'degraded',
      ...(failures.length ? { errorCode: 'cleanup_incomplete', detail: `failed: ${failures.join(', ')}` } : {}),
    });
  }

  function abortProbe(ctx, reason) {
    if (ctx.aborted || ctx.finished) return;
    ctx.aborted = true;
    ctx.abortReason = reason;
    for (const release of ctx.abortWaiters.splice(0)) release();
    ctx.abortRelease?.();
  }

  async function runSeams(ctx) {
    await runStage(ctx, 'config', 'fail', stageConfig);
    await runStage(ctx, 'gateway', 'fail', stageGateway);
    // A slow/absent tmuxctld degrades routing but audio capture still works —
    // degraded, not fail (transcription itself is exercised by openai_ws).
    await runStage(ctx, 'tmuxctld_health', 'degraded', stageTmuxctldHealth);
    await runStage(ctx, 'openai_ws', 'fail', stageOpenaiWs);
  }

  async function runFull(ctx) {
    try {
      const gate = await runStage(ctx, 'operator_gate', 'fail', () => stageOperatorGate(ctx));
      if (ctx.aborted || gate?.ok === false) return;
      const join = await runStage(ctx, 'voice_join', 'fail', () => stageVoiceJoin(ctx));
      if (ctx.aborted || join?.ok === false) return;
      const audio = await runStage(ctx, 'audio_loop', 'fail', () => stageAudioLoop(ctx));
      if (ctx.aborted || audio?.ok === false) return;
      await runStage(ctx, 'tmuxctld_session', 'fail', () => stageTmuxctldSession(ctx));
    } finally {
      await runCleanup(ctx);
    }
  }

  function buildReport(ctx) {
    ctx.finished = true;
    const finishedAt = Date.now();
    const failed = ctx.stages.filter((s) => !s.ok);
    const firstFailed = failed[0]?.stage || null;
    let overall;
    if (ctx.aborted) overall = 'aborted';
    else if (failed.some((s) => s.severity === 'fail')) overall = 'fail';
    else if (failed.length || ctx.degraded) overall = 'degraded';
    else overall = 'pass';
    return {
      contract_version: CONTRACT_VERSION,
      probe_id: ctx.probeId,
      variant: ctx.variant,
      trigger: ctx.trigger,
      started_at: new Date(ctx.startedAt).toISOString(),
      finished_at: new Date(finishedAt).toISOString(),
      duration_ms: finishedAt - ctx.startedAt,
      overall,
      abort_reason: ctx.abortReason || null,
      first_failed_stage: firstFailed,
      stages: ctx.stages.slice(),
      transcript_match: ctx.transcriptMatch || null,
      daemon: { version: daemonVersion || null, pid: process.pid, node: process.version },
    };
  }

  async function surfaceReport(report) {
    // Events row — ALWAYS, fire-and-forget with a hard 3s bound.
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 3_000);
      fetchImpl(`http://127.0.0.1:${config.token_api_port}/api/events/log`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          event_type: 'voice_selftest',
          details: {
            probe_id: report.probe_id,
            variant: report.variant,
            trigger: report.trigger,
            overall: report.overall,
            abort_reason: report.abort_reason,
            duration_ms: report.duration_ms,
            first_failed_stage: report.first_failed_stage,
            stages: report.stages.map(({ stage, ok, ms, errorCode }) => ({ stage, ok, ms, ...(errorCode ? { errorCode } : {}) })),
            transcript_match: report.transcript_match
              ? {
                  matched: report.transcript_match.matched,
                  matched_tokens: report.transcript_match.matched_tokens,
                  attempts: report.transcript_match.attempts,
                  passed_on_retry: report.transcript_match.passed_on_retry,
                }
              : null,
          },
        }),
        signal: controller.signal,
      }).catch(() => {}).finally(() => clearTimeout(timer));
    } catch {}

    // Alerts channel — only fail/degraded/deadline-abort. Operator-caused
    // aborts are silent: the operator taking a VC is normal life, not a fault.
    const alertable =
      report.overall === 'fail' ||
      report.overall === 'degraded' ||
      (report.overall === 'aborted' && report.abort_reason === 'deadline');
    if (!alertable) return;
    const key = `${report.overall}:${report.first_failed_stage || ''}`;
    const nowTs = Date.now();
    if (nowTs - (alertLastSent.get(key) || 0) < alertDedupeMs) return;
    alertLastSent.set(key, nowTs);
    const failedLines = report.stages
      .filter((s) => !s.ok)
      .map((s) => `• \`${s.stage}\` — ${s.errorCode || 'failed'}${s.detail ? `: ${s.detail}` : ''}`);
    const content = [
      `🔴 Voice selftest **${report.overall.toUpperCase()}** (${report.variant}, trigger=${report.trigger}, ${report.duration_ms}ms)`,
      ...failedLines,
    ].join('\n').slice(0, 1900);
    try {
      await postAlert(content);
    } catch (err) {
      logger.warn(`Voice selftest: alert post failed: ${err?.message}`);
    }
  }

  async function run({ variant = 'seams', trigger = 'manual' } = {}) {
    if (active) {
      return { errorCode: 'probe_in_progress', probe_id: active.probeId, running_variant: active.variant };
    }
    probeCounter += 1;
    const ctx = {
      probeId: `probe-${Date.now().toString(36)}-${probeCounter}`,
      variant: variant === 'full' ? 'full' : 'seams',
      trigger: String(trigger || 'manual'),
      startedAt: Date.now(),
      stages: [],
      aborted: false,
      abortReason: null,
      finished: false,
      degraded: false,
      transcriptMatch: null,
      transcriptSink: null,
      abortWaiters: [],
      grantIssued: false,
      joinedListener: false,
      joinedSpeaker: false,
      speakerUserId: null,
      unwatchOperator: null,
      abortRelease: null,
    };
    active = ctx;
    logger.info(`Voice selftest [${ctx.probeId}]: starting ${ctx.variant} probe (trigger=${ctx.trigger})`);

    const deadlineMs = ctx.variant === 'full' ? fullDeadlineMs : seamsDeadlineMs;
    const deadlineTimer = setTimeout(() => abortProbe(ctx, 'deadline'), deadlineMs);
    const abortSignal = new Promise((resolve) => { ctx.abortRelease = resolve; });

    try {
      const runner = (ctx.variant === 'full' ? runFull(ctx) : runSeams(ctx)).catch((err) => {
        logger.warn(`Voice selftest [${ctx.probeId}]: runner error: ${err?.message}`);
      });
      // Hard-deadline abort: if a stage wedges past the deadline, finalize the
      // report anyway. On abort the runner gets a short grace to unwind through
      // its cleanup (the normal operator-abort path finishes well inside it);
      // a truly wedged runner is abandoned — its finally still cleans up
      // whenever it unwedges, and the probe grant TTL-expires regardless.
      await Promise.race([
        runner,
        abortSignal.then(() => Promise.race([
          runner,
          new Promise((resolve) => setTimeout(resolve, abortGraceMs)),
        ])),
      ]);
    } finally {
      clearTimeout(deadlineTimer);
    }

    const report = buildReport(ctx);
    lastReport = report;
    active = null;
    logger.info(
      `Voice selftest [${ctx.probeId}]: ${report.overall} (${report.variant}, ${report.duration_ms}ms` +
      `${report.first_failed_stage ? `, first_failed=${report.first_failed_stage}` : ''})`,
    );
    await surfaceReport(report);
    return report;
  }

  function consumeTranscript(result) {
    // Ship prevention: while a full probe is live, transcripts spoken by the
    // probe speaker on the listener bot are the probe's — they must never
    // reach the router. Everything else (e.g. the operator) passes through.
    if (!active || active.variant !== 'full') return false;
    if (normalizeBot(result?.botName) !== normalizeBot(listenerBot)) return false;
    if (!active.speakerUserId || String(result?.userId || '') !== active.speakerUserId) return false;
    const sink = active.transcriptSink;
    if (sink) sink(result?.text || '');
    return true;
  }

  return {
    run,
    last: () => lastReport,
    consumeTranscript,
  };
}
