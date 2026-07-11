// voice.js — Discord voice channel management
// Handles joining/leaving voice channels, live audio streaming, and TTS playback
// Supports per-bot voice channels with auto-join/leave on operator presence

import {
  joinVoiceChannel,
  VoiceConnectionStatus,
  entersState,
  EndBehaviorType,
  createAudioPlayer,
  createAudioResource,
  AudioPlayerStatus,
  StreamType,
} from '@discordjs/voice';
import { mkdirSync, existsSync, unlinkSync } from 'fs';
import { join } from 'path';
import { Transform } from 'stream';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { Events } from 'discord.js';
import { tmuxctldClient } from './tmuxctld-client.ts';
import prism from 'prism-media';

const execFileAsync = promisify(execFile);

const AUDIO_DIR = join(process.env.HOME || '/tmp', '.discord-cli', 'audio');
mkdirSync(AUDIO_DIR, { recursive: true });

function normalizeBotName(botName) {
  return String(botName || 'unknown').trim().toLowerCase().replaceAll('-', '_');
}

/**
 * @typedef {object} VoiceManager
 * @property {function(string, string=): Promise<object>} joinChannel
 * @property {function(string=, string=): Promise<object>} leaveChannel
 * @property {function(string=): object} startListening
 * @property {function(string=): object} stopListening
 * @property {function(string=): object} getStatus
 * @property {function(): void} setupAutoJoin
 * @property {function(string, string=): Promise<object>} playAudio
 * @property {function(string=): object} stopPlayback
 * @property {function(string, string=, object=): Promise<object>} playTTS
 * @property {function(string=, string=, string=): object} clearLocalVoiceSession
 * @property {function(string, string=, number=): Promise<object>} muteMember
 * @property {function(string, string=): Promise<object>} unmuteMember
 * @property {function(function): void} setAudioFrameCallback
 * @property {function(function): void} setAudioEndCallback
 * @property {function(function): void} setAudioCommitCallback
 * @property {function(function): void} setVoiceLeaveCallback
 * @property {function(string=): Promise<object>} reconcileOperatorVoiceState
 */

/**
 * @returns {VoiceManager}
 */
export function createVoiceManager(botClients, config, logger) {
  const guildId = config.guild_id;
  const operatorUserId = config.operator_user_id;
  const voiceChannels = config.voice_channels || {};

  // Per-bot connection state: botName -> { connection, listening, subscriptions, channelId }
  const botStates = new Map();
  const muteTimers = new Map();

  // Collect all bot user IDs to filter out of audio capture (prevent ouroboros)
  // Populated lazily after bots connect
  const botUserIds = new Set();

  function refreshBotUserIds() {
    for (const client of Object.values(botClients)) {
      const id = client.botUserId || client.client?.user?.id;
      if (id) botUserIds.add(id);
    }
  }

  // Realtime transcription callbacks — set externally
  let onAudioFrame = null;
  let onAudioEnd = null;
  let onAudioCommit = null;
  let onVoiceLeave = null;
  const SILENCE_PCM_20MS = Buffer.alloc(48000 / 50 * 2);

  function getBotState(botName) {
    if (!botStates.has(botName)) {
      botStates.set(botName, {
        connection: null,
        listening: false,
        activeSubscriptions: new Map(),
        channelId: null,
        joining: false,
        player: null,       // AudioPlayer for playback
        playing: false,      // Currently playing audio
        playChain: Promise.resolve(),  // per-bot playback mutex (serialize plays)
        playGeneration: 0,   // bumped by stopPlayback to invalidate queued plays
        leaveTimer: null,
        routeEpoch: 0,
      });
    }
    return botStates.get(botName);
  }

  function getClient(botName = 'mechanicus') {
    return botClients[botName];
  }

  function connectionUsable(state) {
    const status = state.connection?.state?.status;
    if (!state.connection) return false;
    if (status === 'destroyed' || status === 'disconnected') {
      state.connection = null;
      state.listening = false;
      state.channelId = null;
      return false;
    }
    return true;
  }


  async function joinChannel(voiceChannelId, botName = 'mechanicus') {
    const client = getClient(botName);
    if (!client?.client) throw new Error(`Bot '${botName}' not available`);

    const state = getBotState(botName);
    if (state.joining) {
      throw new Error(`Bot '${botName}' is already joining a voice channel`);
    }
    state.joining = true;

    try {
      const guild = await client.client.guilds.fetch(guildId);
      const channel = await guild.channels.fetch(voiceChannelId);

      if (!channel?.isVoiceBased?.()) {
        throw new Error(`Channel ${voiceChannelId} is not a voice channel`);
      }

      // Destroy existing connection if any through the same leave cleanup path
      // used by auto-leave, VC hops, and manual disconnects.
      if (connectionUsable(state)) {
        await leaveChannel(botName, 'rejoin');
      } else if (state.connection) {
        try { state.connection.destroy(); } catch {}
        state.connection = null;
      }
      if (state.leaveTimer) {
        clearTimeout(state.leaveTimer);
        state.leaveTimer = null;
      }

      state.connection = joinVoiceChannel({
        channelId: voiceChannelId,
        guildId: guildId,
        adapterCreator: guild.voiceAdapterCreator,
        selfDeaf: false, // MUST be false to receive audio
        selfMute: false, // Unmuted to support audio playback
      });

      state.channelId = voiceChannelId;

      // Wait for connection to be ready
      try {
        await entersState(state.connection, VoiceConnectionStatus.Ready, 10_000);
        logger.info(`Voice [${botName}]: joined channel ${channel.name} (${voiceChannelId})`);
      } catch (err) {
        // state.connection may have been nulled by a concurrent leaveChannel during the wait
        if (state.connection) state.connection.destroy();
        state.connection = null;
        state.channelId = null;
        throw new Error(`Failed to join voice channel: ${err.message}`);
      }

      // Set up speaking detection for auto-subscribe
      // Refresh bot IDs on each join (bots may have connected since last check)
      refreshBotUserIds();

      state.connection.receiver.speaking.on('start', (userId) => {
        if (!state.listening) return;
        if (state.activeSubscriptions.has(userId)) return;
        // Ignore other bots to prevent ouroboros (bot transcribing its own TTS or other bots)
        if (botUserIds.has(userId)) {
          logger.debug(`Voice [${botName}]: ignoring bot user ${userId}`);
          return;
        }
        logger.info(`Voice [${botName}]: user ${userId} started speaking, subscribing...`);
        subscribeToUser(botName, userId);
      });

      return { channelId: voiceChannelId, channelName: channel.name, botName };
    } finally {
      state.joining = false;
    }
  }

  // Explicit commit after Discord silence. Audio itself is streamed live to
  // OpenAI Realtime; no local PCM chunk files are created.
  const SILENCE_COMMIT_MS = config.voice_silence_commit_ms ?? 700; // Wait after Discord silence before committing a turn.
  const MIN_LOCAL_COMMIT_AUDIO_MS = config.voice_min_commit_audio_ms ?? 100;

  function subscribeToUser(botName, userId) {
    const state = getBotState(botName);
    if (!state.connection) return;

    // Use Manual end behavior — we manage the stream lifetime ourselves.
    // AfterSilence kills the stream and Discord won't re-fire speaking.start
    // reliably for the same user, causing lost audio on subsequent utterances.
    const audioStream = state.connection.receiver.subscribe(userId, {
      end: { behavior: EndBehaviorType.Manual },
    });

    // Filter out Discord silence frames before they hit the Opus decoder.
    // During silence, Discord sends padding frames (0xF8 0xFF 0xFE etc.)
    // that corrupt the decoder. We filter these and use them as silence signals.
    // The last forwarded frame is kept for decoder-error diagnostics.
    let lastOpusFrame = null;
    const silenceFilter = new Transform({
      transform(chunk, encoding, callback) {
        // Discord silence frames are ≤5 bytes (typically 3 bytes: 0xF8 0xFF 0xFE).
        // Speech frames are 40-80+ bytes. Filter silence to prevent Opus decoder corruption.
        if (chunk.length <= 5) {
          silenceFilter.emit('silence');
          callback();
        } else {
          lastOpusFrame = chunk;
          callback(null, chunk);
        }
      }
    });

    let hasAudioSinceCommit = false;
    let bytesSinceCommit = 0;
    let silenceTimer = null;
    let voiceSessionId = null;
    let voiceSessionStartInFlight = false;
    let voiceSessionGeneration = 0;
    let pendingCommitRequest = null;
    let discarded = false;
    let suppressEndClose = false;

    function currentRouteMeta(extra = {}) {
      return {
        routeEpoch: state.routeEpoch,
        channelId: state.channelId,
        voice_session_id: voiceSessionId,
        ...extra,
      };
    }

    function downsampledDurationMs(bytes48kMonoS16) {
      const downsampledBytes = Math.floor(Number(bytes48kMonoS16 || 0) / 2);
      return (downsampledBytes / (24000 * 2)) * 1000;
    }

    function commitPending(reason, extra = {}) {
      if (silenceTimer) {
        clearTimeout(silenceTimer);
        silenceTimer = null;
      }
      if (voiceSessionStartInFlight) {
        pendingCommitRequest = { reason, extra };
        logger.debug?.(
          `Voice [${botName}]: deferring realtime audio commit from ${userId} ` +
          `until voice session start completes (reason=${reason})`
        );
        return true;
      }
      if (!hasAudioSinceCommit) return false;
      const audioMs = downsampledDurationMs(bytesSinceCommit);
      if (audioMs < MIN_LOCAL_COMMIT_AUDIO_MS) {
        logger.debug?.(
          `Voice [${botName}]: skipping tiny realtime audio commit from ${userId} ` +
          `(${Math.round(audioMs)}ms, ${bytesSinceCommit} bytes, reason=${reason})`
        );
        hasAudioSinceCommit = false;
        bytesSinceCommit = 0;
        return false;
      }
      logger.info(`Voice [${botName}]: committing realtime audio from ${userId} (${bytesSinceCommit} bytes, reason=${reason})`);
      if (onAudioCommit) {
        try { onAudioCommit(userId, botName, currentRouteMeta({ reason, audioMs, ...extra })); } catch {}
      }
      hasAudioSinceCommit = false;
      bytesSinceCommit = 0;
      return true;
    }

    function discardPending() {
      suppressEndClose = true;
      if (silenceTimer) {
        clearTimeout(silenceTimer);
        silenceTimer = null;
      }
      hasAudioSinceCommit = false;
      bytesSinceCommit = 0;
      voiceSessionId = null;
      voiceSessionStartInFlight = false;
      voiceSessionGeneration += 1;
      pendingCommitRequest = null;
      discarded = true;
    }

    function clearLocalVoiceSession(expectedVoiceSessionId = '') {
      if (expectedVoiceSessionId && voiceSessionId && voiceSessionId !== expectedVoiceSessionId) {
        return false;
      }
      if (!voiceSessionId && !voiceSessionStartInFlight) return false;
      voiceSessionId = null;
      voiceSessionGeneration += 1;
      return true;
    }

    function startSilenceTimer() {
      if (silenceTimer) clearTimeout(silenceTimer);
      silenceTimer = setTimeout(() => {
        commitPending('silence', { silenceMs: SILENCE_COMMIT_MS });
      }, SILENCE_COMMIT_MS);
    }

    // Silence frames from Discord trigger the local commit timer only. Do not
    // append synthetic silence into Realtime: it can create empty sessions after
    // cleanup and swallow the next short utterance.
    silenceFilter.on('silence', () => {
      if (hasAudioSinceCommit) {
        startSilenceTimer();
      }
    });

    function onDecodedAudio(chunk) {
      if (discarded) return;
      // First real audio frame of a local utterance: tmuxctld creates the
      // semantic voice session. Discord carries only the opaque session id.
      if (!hasAudioSinceCommit && !voiceSessionId && !voiceSessionStartInFlight) {
        const startGeneration = voiceSessionGeneration + 1;
        voiceSessionGeneration = startGeneration;
        voiceSessionStartInFlight = true;
        tmuxctldClient.startVoiceSession({
          botName: normalizeBotName(botName),
          userId,
          channelId: state.channelId,
          routeEpoch: state.routeEpoch,
        }).then((started) => {
          if (discarded || voiceSessionGeneration !== startGeneration) {
            const staleId = started.voice_session_id || '';
            if (staleId) {
              tmuxctldClient.clearVoiceSession({ voiceSessionId: staleId }).catch(() => {});
            }
            return;
          }
          voiceSessionId = started.voice_session_id || null;
          logger.info(`Voice [${botName}]: started voice session ${voiceSessionId || 'none'} for user ${userId}`);
        }).catch((err) => {
          if (discarded || voiceSessionGeneration !== startGeneration) return;
          logger.warn(`Voice [${botName}]: voice session start failed for user ${userId}: ${err.code || err.message}`);
        }).finally(() => {
          if (!discarded) {
            voiceSessionStartInFlight = false;
            if (!voiceSessionId) {
              pendingCommitRequest = null;
              hasAudioSinceCommit = false;
              bytesSinceCommit = 0;
              return;
            }
            const pending = pendingCommitRequest;
            pendingCommitRequest = null;
            if (pending && hasAudioSinceCommit) {
              commitPending(pending.reason, pending.extra);
            }
          }
        });
      }

      if (onAudioFrame) {
        try { onAudioFrame(userId, chunk, botName, currentRouteMeta({ silence: false })); } catch {}
      }
      // Real audio arrived — cancel any pending silence commit.
      if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }

      hasAudioSinceCommit = true;
      bytesSinceCommit += chunk.length;
    }

    // A single corrupted Opus frame must not cost the rest of the utterance or
    // the session: recreate the decoder in place (bounded per subscription) and
    // keep every per-utterance closure state — voice session, commit counters,
    // the live Realtime stream — intact. Only exhaustion tears the subscription
    // down and pages via logger.error.
    const MAX_DECODER_RECOVERIES = 3;
    let decoderRecoveries = 0;

    const sub = {
      stream: audioStream,
      decoder: null,
      commit: commitPending,
      discard: discardPending,
      clearVoiceSession: clearLocalVoiceSession,
    };

    function frameDiagnostics() {
      if (!lastOpusFrame) return 'frame=none';
      return `frame_len=${lastOpusFrame.length} frame_head=${lastOpusFrame.subarray(0, 8).toString('hex')}`;
    }

    function teardownSubscription() {
      state.activeSubscriptions.delete(userId);
      if (!suppressEndClose && onAudioEnd) {
        try { onAudioEnd(userId, botName); } catch {}
      }
    }

    function makeDecoder() {
      // Decode Opus → PCM (48kHz mono s16le)
      const dec = new prism.opus.Decoder({
        rate: 48000,
        channels: 1, // Mono for transcription
        frameSize: 960,
      });

      dec.on('data', onDecodedAudio);

      // Stream only ends on manual destroy (leave/stop) — we handle that in leaveChannel
      dec.on('end', () => {
        if (silenceTimer) clearTimeout(silenceTimer);
        state.activeSubscriptions.delete(userId);
        commitPending('stream-end');
        if (!suppressEndClose && onAudioEnd) {
          try { onAudioEnd(userId, botName); } catch {}
        }
      });

      dec.on('error', (err) => {
        if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }
        const live = !discarded && state.activeSubscriptions.get(userId) === sub;
        if (live && decoderRecoveries < MAX_DECODER_RECOVERIES) {
          decoderRecoveries += 1;
          logger.warn(
            `Voice [${botName}]: decoder error for ${userId}: ${err.message} — ` +
            `recreating decoder (recovery ${decoderRecoveries}/${MAX_DECODER_RECOVERIES}, ${frameDiagnostics()})`,
            { errorCode: 'opus_decode_failed', botName, userId, recovered: true },
          );
          try { silenceFilter.unpipe(dec); } catch {}
          try { dec.destroy(); } catch {}
          sub.decoder = makeDecoder();
          silenceFilter.pipe(sub.decoder);
          return;
        }
        if (!live) {
          // Teardown/hop race: the subscription is already discarded or replaced.
          logger.warn(
            `Voice [${botName}]: decoder error for ${userId} after subscription teardown: ${err.message}`,
            { errorCode: 'opus_decode_failed', botName, userId, recovered: false },
          );
          teardownSubscription();
          return;
        }
        logger.error(
          `Voice [${botName}]: decoder error for ${userId}: ${err.message} ` +
          `(recoveries exhausted ${decoderRecoveries}/${MAX_DECODER_RECOVERIES}, ${frameDiagnostics()})`,
          { errorCode: 'opus_decode_failed', botName, userId, recovered: false },
        );
        teardownSubscription();
      });

      return dec;
    }

    sub.decoder = makeDecoder();
    audioStream.pipe(silenceFilter).pipe(sub.decoder);

    audioStream.on('error', (err) => {
      if (silenceTimer) clearTimeout(silenceTimer);
      logger.error(
        `Voice [${botName}]: stream error for ${userId}: ${err.message}`,
        { errorCode: 'voice_audio_stream_error', botName, userId },
      );
      teardownSubscription();
    });

    state.activeSubscriptions.set(userId, sub);
  }

  function clearLocalVoiceSession(botName = 'mechanicus', userId = '', voiceSessionId = '') {
    const state = getBotState(botName);
    const sub = state.activeSubscriptions.get(String(userId || ''));
    if (!sub?.clearVoiceSession) return { cleared: false, reason: 'no_subscription', botName, userId };
    const cleared = sub.clearVoiceSession(String(voiceSessionId || ''));
    return { cleared, botName, userId, voiceSessionId };
  }

  async function runVoiceLeaveCleanup(botName, meta = {}) {
    if (!onVoiceLeave) return;
    try {
      await onVoiceLeave(botName, meta);
    } catch (err) {
      logger.warn(`Voice [${botName}]: leave cleanup failed: ${err.message}`);
    }
  }

  function cleanupActiveSubscriptions(state, botName, reason, { commit = true } = {}) {
    // Normal stop may commit pending audio. VC leave/hop discards it: the
    // operator has moved away, and stale transcripts must not land afterward.
    for (const [userId, sub] of state.activeSubscriptions) {
      if (commit && sub.commit) {
        try { sub.commit(reason); } catch {}
      } else if (sub.discard) {
        try { sub.discard(); } catch {}
      }
      try { sub.stream.destroy(); } catch {}
      try { sub.decoder.destroy(); } catch {}
    }
    state.activeSubscriptions.clear();
  }

  async function leaveChannel(botName = 'mechanicus', reason = 'manual') {
    const state = getBotState(botName);
    const leftChannel = state.channelId;
    if (!connectionUsable(state)) {
      return { left: false, reason: 'not connected', channelId: leftChannel, botName };
    }

    cleanupActiveSubscriptions(state, botName, reason, { commit: false });

    try { state.connection.destroy(); } catch (err) { logger.warn(`Voice [${botName}]: destroy during leave ignored: ${err.message}`); }
    state.connection = null;
    state.listening = false;
    state.channelId = null;

    logger.info(`Voice [${botName}]: left channel ${leftChannel} (${reason})`);
    await runVoiceLeaveCleanup(botName, { channelId: leftChannel, reason, left: true, botName });
    return { left: true, channelId: leftChannel, botName, reason };
  }

  function startListening(botName = 'mechanicus') {
    const state = getBotState(botName);
    if (!state.connection) throw new Error(`Bot '${botName}' not connected to a voice channel`);
    state.listening = true;
    logger.info(`Voice [${botName}]: listening started`);
    return { listening: true, channelId: state.channelId, botName };
  }

  function stopListening(botName = 'mechanicus') {
    const state = getBotState(botName);
    state.listening = false;
    // Commit and clean up active subscriptions.
    for (const [userId, sub] of state.activeSubscriptions) {
      if (sub.commit) {
        try { sub.commit('stop'); } catch {}
      }
      try { sub.stream.destroy(); } catch {}
      try { sub.decoder.destroy(); } catch {}
    }
    state.activeSubscriptions.clear();
    logger.info(`Voice [${botName}]: listening stopped`);
    return { listening: false, channelId: state.channelId, botName };
  }

  function getStatus(botName) {
    // If botName specified, return that bot's status
    if (botName) {
      const state = getBotState(botName);
      return {
        botName,
        connected: connectionUsable(state),
        channelId: state.channelId,
        listening: state.listening,
        activeListeners: state.activeSubscriptions.size,
        connectionState: state.connection?.state?.status || 'disconnected',
        routeEpoch: state.routeEpoch,
      };
    }
    // Return all bots' status
    const statuses = {};
    for (const [name, state] of botStates) {
      // connectionUsable() reflects whether audio can actually be delivered —
      // it also nulls out destroyed/disconnected connections. Using the raw
      // `!!state.connection` here used to report a dead pipe as connected,
      // which let Token-API route TTS to nobody and claim success.
      statuses[name] = {
        connected: connectionUsable(state),
        channelId: state.channelId,
        listening: state.listening,
        activeListeners: state.activeSubscriptions.size,
        connectionState: state.connection?.state?.status || 'disconnected',
        routeEpoch: state.routeEpoch,
      };
    }
    // Include configured but not-yet-connected bots
    for (const name of Object.keys(voiceChannels)) {
      if (!statuses[name]) {
        statuses[name] = {
          connected: false,
          channelId: null,
          listening: false,
          activeListeners: 0,
          connectionState: 'disconnected',
          routeEpoch: 0,
          assignedChannel: voiceChannels[name],
        };
      }
    }
    return statuses;
  }

  async function muteMember(userId = operatorUserId, botName = 'mechanicus', durationMs = config.voice_command_mute_ms ?? 15000) {
    if (!userId) throw new Error('user_id required');
    const client = botClients[botName]?.client;
    if (!client) throw new Error(`Bot '${botName}' client not available`);
    const guild = await client.guilds.fetch(guildId);
    const member = await guild.members.fetch(userId);
    if (!member?.voice?.channelId) {
      return { muted: false, reason: 'member_not_in_voice', userId, botName };
    }

    const key = `${guildId}:${userId}`;
    if (muteTimers.has(key)) clearTimeout(muteTimers.get(key));

    await member.voice.setMute(true, 'Voice command: temporary mute');
    logger.info(`Voice [${botName}]: server-muted member ${userId} for ${durationMs}ms`);

    const timer = setTimeout(async () => {
      muteTimers.delete(key);
      try {
        const fresh = await guild.members.fetch(userId);
        if (fresh?.voice?.serverMute) {
          await fresh.voice.setMute(false, 'Voice command: temporary mute expired');
          logger.info(`Voice [${botName}]: temporary mute expired for member ${userId}`);
        }
      } catch (err) {
        logger.warn(`Voice [${botName}]: temporary unmute failed for ${userId}: ${err.message}`);
      }
    }, Math.max(1000, Number(durationMs) || 15000));
    muteTimers.set(key, timer);

    return { muted: true, temporary: true, durationMs, userId, botName, channelId: member.voice.channelId };
  }

  async function unmuteMember(userId = operatorUserId, botName = 'mechanicus') {
    if (!userId) throw new Error('user_id required');
    const client = botClients[botName]?.client;
    if (!client) throw new Error(`Bot '${botName}' client not available`);
    const guild = await client.guilds.fetch(guildId);
    const key = `${guildId}:${userId}`;
    if (muteTimers.has(key)) {
      clearTimeout(muteTimers.get(key));
      muteTimers.delete(key);
    }
    const member = await guild.members.fetch(userId);
    if (!member?.voice?.channelId) {
      return { unmuted: false, reason: 'member_not_in_voice', userId, botName };
    }
    await member.voice.setMute(false, 'Voice command: unmute');
    logger.info(`Voice [${botName}]: server-unmuted member ${userId}`);
    return { unmuted: true, userId, botName, channelId: member.voice.channelId };
  }

  let operatorVoiceChannelId = null;

  function botNameForChannel(channelId) {
    if (!channelId) return null;
    const match = Object.entries(voiceChannels).find(([, assignedChannel]) => assignedChannel === channelId);
    return match ? match[0] : null;
  }

  async function joinAndListenIfCurrent(botName, channelId, routeEpoch) {
    const state = getBotState(botName);
    try {
      await joinChannel(channelId, botName);
      if (state.routeEpoch !== routeEpoch || operatorVoiceChannelId !== channelId) {
        logger.warn(
          `Voice auto-join [${botName}]: stale join completed for ${channelId}; ` +
          `operator now in ${operatorVoiceChannelId || 'none'}, leaving immediately`
        );
        const left = await leaveChannel(botName, 'stale-join');
        if (!left.left) {
          await runVoiceLeaveCleanup(botName, {
            channelId,
            reason: 'stale-join-invalidation',
            left: false,
            botName,
          });
        }
        return;
      }
      startListening(botName);
      logger.info(`Voice auto-join [${botName}]: joined and listening`);
    } catch (err) {
      logger.error(`Voice auto-join [${botName}]: failed to join: ${err.message}`);
    }
  }

  async function leaveBotNow(botName, reason, expectedChannelId = null) {
    const state = getBotState(botName);
    state.routeEpoch += 1;
    if (state.leaveTimer) {
      clearTimeout(state.leaveTimer);
      state.leaveTimer = null;
    }
    if (!connectionUsable(state)) {
      if (state.joining) {
        logger.info(`Voice auto-join [${botName}]: invalidated in-flight join (${reason})`);
      }
      logger.info(`Voice auto-join [${botName}]: running leave cleanup without live connection (${reason})`);
      await runVoiceLeaveCleanup(botName, {
        channelId: state.channelId || expectedChannelId,
        reason,
        left: false,
        invalidatedJoin: true,
        botName,
      });
      return;
    }
    logger.info(`Voice auto-join [${botName}]: leaving immediately (${reason})`);
    try {
      await leaveChannel(botName, reason);
    } catch (err) {
      logger.error(`Voice auto-join [${botName}]: failed to leave: ${err.message}`);
    }
  }

  function scheduleBotLeave(botName, channelId, reason) {
    const state = getBotState(botName);
    state.routeEpoch += 1;
    const invalidatedEpoch = state.routeEpoch;
    const graceMs = Number(config.voice_auto_leave_grace_ms ?? 5000);
    if (state.leaveTimer) clearTimeout(state.leaveTimer);

    // Leave intent cleanup is immediate. The Discord connection may stay alive
    // for grace, but drafts, overlays, Realtime sessions, and pending audio are stale now.
    cleanupActiveSubscriptions(state, botName, reason, { commit: false });
    state.listening = false;
    void runVoiceLeaveCleanup(botName, {
      channelId,
      reason,
      left: false,
      intent: 'leave',
      routeEpoch: invalidatedEpoch,
      botName,
    });

    if (!connectionUsable(state)) {
      if (state.joining) {
        logger.info(`Voice auto-join [${botName}]: invalidated in-flight join (${reason})`);
      }
      return;
    }
    logger.info(`Voice auto-join [${botName}]: operator left ${channelId}, disconnecting after ${graceMs}ms grace...`);
    state.leaveTimer = setTimeout(async () => {
      state.leaveTimer = null;
      if (!connectionUsable(state)) return;
      try {
        await leaveChannel(botName, reason);
      } catch (err) {
        logger.error(`Voice auto-join [${botName}]: failed to leave: ${err.message}`);
      }
    }, Math.max(0, graceMs));
  }

  async function syncAutoJoinForOperatorChannel(trigger, { immediateLeave = false } = {}) {
    const desiredBotName = botNameForChannel(operatorVoiceChannelId);

    // Invalidate/cleanup non-current bots before starting any new join. This
    // makes VC hops deterministic even if Discord omits oldState.channelId or
    // voice_channels object order has the new bot first.
    for (const [botName, channelId] of Object.entries(voiceChannels)) {
      const desired = desiredBotName === botName;
      if (desired) continue;
      const state = getBotState(botName);
      const connectedElsewhere = connectionUsable(state);
      const joiningElsewhere = state.joining;
      if (!connectedElsewhere && !joiningElsewhere) continue;

      if (immediateLeave || operatorVoiceChannelId) {
        await leaveBotNow(botName, `operator in ${operatorVoiceChannelId || 'no assigned channel'} via ${trigger}`);
      } else {
        scheduleBotLeave(botName, channelId, trigger);
      }
    }

    if (!desiredBotName) return;

    const channelId = voiceChannels[desiredBotName];
    const state = getBotState(desiredBotName);
    if (state.leaveTimer) {
      clearTimeout(state.leaveTimer);
      state.leaveTimer = null;
    }
    if (connectionUsable(state) && state.channelId === channelId) {
      logger.debug(`Voice auto-join [${desiredBotName}]: already connected to current channel (${trigger})`);
      return;
    }
    if (state.joining) {
      logger.debug(`Voice auto-join [${desiredBotName}]: already joining current channel (${trigger})`);
      return;
    }
    state.routeEpoch += 1;
    const routeEpoch = state.routeEpoch;
    logger.info(`Voice auto-join [${desiredBotName}]: operator joined ${channelId}, following... (${trigger})`);
    void joinAndListenIfCurrent(desiredBotName, channelId, routeEpoch);
  }

  async function leaveBotForChannel(channelId, reason) {
    const botName = botNameForChannel(channelId);
    if (!botName) return false;
    await leaveBotNow(botName, reason, channelId);
    return true;
  }

  /**
   * Set up auto-join/leave for all bots that have voice_channels configured.
   * A single operator voice-state listener owns the routing state for every bot.
   * This matters for direct VC hops: Discord can deliver "joined B" while the
   * old bot is still joining/leaving A, so stale joins are invalidated and any
   * non-current bot leaves immediately instead of waiting for the normal grace.
   */
  function setupAutoJoin() {
    const eventClient = Object.values(botClients).find(c => c?.client);
    if (!eventClient?.client) {
      logger.warn('Voice auto-join: no bot clients available, skipping');
      return;
    }

    for (const [botName, channelId] of Object.entries(voiceChannels)) {
      const client = getClient(botName);
      if (!client?.client) {
        logger.warn(`Voice auto-join: bot '${botName}' not available, skipping`);
        continue;
      }
      logger.info(`Voice auto-join [${botName}]: watching for operator in channel ${channelId}`);
    }

    // Need GuildVoiceStates intent to receive voiceStateUpdate.
    eventClient.client.on(Events.VoiceStateUpdate, async (oldState, newState) => {
      // Only care about the operator.
      if (newState.member?.id !== operatorUserId && oldState.member?.id !== operatorUserId) return;

      const joinedChannel = newState.channelId;
      const leftChannel = oldState.channelId;
      const previousChannel = operatorVoiceChannelId;
      const explicitChannelSwitch = Boolean(leftChannel && joinedChannel && leftChannel !== joinedChannel);

      // Treat "joined a different VC" as a hop even when Discord did not give
      // the expected oldState channel. This covers hot swaps and cache misses.
      const connectedNonCurrentBot = Object.entries(voiceChannels).some(([botName, channelId]) => {
        const state = getBotState(botName);
        return channelId !== joinedChannel && (connectionUsable(state) || state.joining);
      });
      const isHop = Boolean(
        joinedChannel &&
        joinedChannel !== leftChannel &&
        (leftChannel || (previousChannel && previousChannel !== joinedChannel) || connectedNonCurrentBot)
      );

      operatorVoiceChannelId = joinedChannel || null;

      logger.info(
        `Voice auto-join: operator voice update left=${leftChannel || 'none'} ` +
        `joined=${joinedChannel || 'none'} previous=${previousChannel || 'none'} hop=${isHop}`
      );

      if (explicitChannelSwitch) {
        await leaveBotForChannel(leftChannel, `explicit-vc-hop ${leftChannel}->${joinedChannel}`);
      }

      await syncAutoJoinForOperatorChannel(isHop ? 'vc-hop' : 'voice-state-update', {
        immediateLeave: isHop || Boolean(joinedChannel),
      });
    });
  }

  /**
   * Startup reconciliation for the common case where the operator is already
   * in a configured VC before this daemon finishes connecting. In that case
   * Discord does not emit a fresh VoiceStateUpdate, so auto-join never fires.
   */
  async function reconcileOperatorVoiceState() {
    if (!operatorUserId) {
      logger.warn('Voice startup sync: no operator_user_id configured');
      return { joined: false, reason: 'missing_operator_user_id' };
    }

    const lookupClient = Object.values(botClients).find(c => c?.client);
    if (!lookupClient?.client) {
      logger.warn('Voice startup sync: no connected bot client available');
      return { joined: false, reason: 'no_client' };
    }

    let currentChannelId = null;
    try {
      const guild = await lookupClient.client.guilds.fetch(guildId);
      const member = await guild.members.fetch(operatorUserId);
      currentChannelId = member.voice?.channelId || guild.voiceStates.cache.get(operatorUserId)?.channelId || null;
    } catch (err) {
      logger.warn(`Voice startup sync: failed to fetch operator voice state: ${err.message}`);
      return { joined: false, reason: 'fetch_failed', error: err.message };
    }

    if (!currentChannelId) {
      logger.info('Voice startup sync: operator is not in a voice channel; waiting for auto-join event');
      return { joined: false, reason: 'operator_not_in_voice' };
    }

    operatorVoiceChannelId = currentChannelId;
    const match = Object.entries(voiceChannels).find(([, channelId]) => channelId === currentChannelId);
    if (!match) {
      logger.info(`Voice startup sync: operator is in unassigned channel ${currentChannelId}; leaving assigned bots idle`);
      await syncAutoJoinForOperatorChannel('startup-unassigned', { immediateLeave: true });
      return { joined: false, reason: 'unassigned_channel', channelId: currentChannelId };
    }

    const [botName, channelId] = match;
    const state = getBotState(botName);
    if (connectionUsable(state) && state.channelId === channelId) {
      logger.info(`Voice startup sync [${botName}]: already connected to ${state.channelId}`);
      await syncAutoJoinForOperatorChannel('startup-current', { immediateLeave: true });
      return { joined: false, reason: 'already_connected', botName, channelId: state.channelId };
    }

    logger.info(`Voice startup sync [${botName}]: operator already in ${channelId}, reconciling...`);
    await syncAutoJoinForOperatorChannel('startup-sync', { immediateLeave: true });
    return { joined: true, botName, channelId };
  }

  // --- Audio Playback ---

  function getOrCreatePlayer(botName) {
    const state = getBotState(botName);
    if (state.player) return state.player;

    const player = createAudioPlayer();

    player.on(AudioPlayerStatus.Playing, () => {
      state.playing = true;
      logger.info(`Voice [${botName}]: playback started`);
    });

    player.on(AudioPlayerStatus.Idle, () => {
      state.playing = false;
      logger.debug(`Voice [${botName}]: playback idle`);
    });

    player.on('error', (err) => {
      state.playing = false;
      logger.error(`Voice [${botName}]: player error: ${err.message}`);
    });

    state.player = player;
    return player;
  }

  /**
   * Play an audio file through a bot's voice connection.
   * Supports: WAV, MP3, OGG, AIFF, and raw PCM (s16le 48kHz mono).
   *
   * Serialized per bot via a promise-chain mutex: `playAudioNow` already awaits
   * AudioPlayerStatus.Idle, but nothing stopped a SECOND concurrent caller from
   * invoking `player.play()` mid-line on the shared player → two overlapping
   * voices. Chaining each play behind the previous makes "one voice per bot"
   * structurally true even if something bypasses the single server-side queue
   * (defense-in-depth under PR A). playTTS routes through here too.
   *
   * `stopPlayback()` bumps `playGeneration`; a queued call captured under an older
   * generation skips instead of starting, so "stop" drains the backlog (silence)
   * rather than resuming with the next queued line after the forced Idle.
   */
  async function playAudio(filePath, botName = 'mechanicus') {
    const state = getBotState(botName);
    const generation = state.playGeneration;
    const run = state.playChain.then(() => {
      if (state.playGeneration !== generation) {
        return { skipped: true, reason: 'stopped', file: filePath, botName };
      }
      return playAudioNow(filePath, botName);
    });
    // Keep the chain alive regardless of this call's success/failure so one
    // rejected play never poisons subsequent plays.
    state.playChain = run.then(() => {}, () => {});
    return run;
  }

  async function playAudioNow(filePath, botName = 'mechanicus') {
    const state = getBotState(botName);
    if (!connectionUsable(state)) {
      throw new Error(`Bot '${botName}' not connected to a voice channel`);
    }

    if (!existsSync(filePath)) {
      throw new Error(`Audio file not found: ${filePath}`);
    }

    const player = getOrCreatePlayer(botName);

    // Subscribe the connection to this player (idempotent)
    state.connection.subscribe(player);

    // Determine input type from extension
    const ext = filePath.split('.').pop().toLowerCase();
    let resource;

    if (ext === 'pcm') {
      // Raw PCM: s16le 48kHz mono — wrap in ffmpeg to produce Opus
      resource = createAudioResource(filePath, {
        inputType: StreamType.Raw,
      });
    } else {
      // WAV, MP3, OGG, AIFF — discord.js/voice handles via ffmpeg
      resource = createAudioResource(filePath);
    }

    // Wait for completion
    return new Promise((resolve, reject) => {
      const onIdle = () => {
        cleanup();
        resolve({ played: true, file: filePath, botName });
      };
      const onError = (err) => {
        cleanup();
        reject(err);
      };
      function cleanup() {
        player.removeListener(AudioPlayerStatus.Idle, onIdle);
        player.removeListener('error', onError);
      }

      player.on(AudioPlayerStatus.Idle, onIdle);
      player.on('error', onError);
      player.play(resource);

      logger.info(`Voice [${botName}]: playing ${filePath}`);
    });
  }

  function stopPlayback(botName = 'mechanicus') {
    const state = getBotState(botName);
    // Invalidate any queued plays so the backlog drains instead of resuming after
    // the forced Idle, and reset the chain so the next play starts clean. "Stop"
    // must mean silence, not "play the next queued line."
    state.playGeneration += 1;
    state.playChain = Promise.resolve();
    if (!state.player) return { stopped: false, reason: 'no player' };
    state.player.stop(true);
    state.playing = false;
    logger.info(`Voice [${botName}]: playback stopped`);
    return { stopped: true, botName };
  }

  /**
   * Generate TTS audio via macOS `say` and play through Discord voice.
   * Creates a temporary AIFF file, plays it, then cleans up.
   */
  async function playTTS(message, botName = 'mechanicus', opts = {}) {
    const state = getBotState(botName);
    if (!connectionUsable(state)) {
      throw new Error(`Bot '${botName}' not connected to a voice channel`);
    }

    const voice = opts.voice || 'Daniel';
    const rate = opts.rate || 190;
    const timestamp = Date.now();
    const outFile = join(AUDIO_DIR, `tts-${botName}-${timestamp}.aiff`);

    // Generate TTS to file using macOS say
    try {
      await execFileAsync('say', [
        '-v', voice,
        '-r', String(rate),
        '-o', outFile,
        message,
      ], { timeout: 30_000 });
    } catch (err) {
      throw new Error(`TTS generation failed: ${err.message}`);
    }

    logger.info(`Voice [${botName}]: TTS generated ${outFile} (${message.length} chars, voice=${voice})`);

    // Play the generated file
    try {
      const result = await playAudio(outFile, botName);
      // Clean up temp file after playback
      try { unlinkSync(outFile); } catch {}
      return { ...result, tts: true, voice, message: message.slice(0, 80) };
    } catch (err) {
      // Clean up on error too
      try { unlinkSync(outFile); } catch {}
      throw err;
    }
  }

  return {
    joinChannel,
    leaveChannel,
    startListening,
    stopListening,
    getStatus,
    setupAutoJoin,
    playAudio,
    stopPlayback,
    playTTS,
    clearLocalVoiceSession,
    muteMember,
    unmuteMember,
    setAudioFrameCallback(cb) { onAudioFrame = cb; },
    setAudioEndCallback(cb) { onAudioEnd = cb; },
    setAudioCommitCallback(cb) { onAudioCommit = cb; },
    setVoiceLeaveCallback(cb) { onVoiceLeave = cb; },
    reconcileOperatorVoiceState,
  };
}
