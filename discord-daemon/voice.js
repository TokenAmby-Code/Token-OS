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
import { execFile, execFileSync } from 'child_process';
import { promisify } from 'util';
import { Events } from 'discord.js';
import prism from 'prism-media';

const execFileAsync = promisify(execFile);

const AUDIO_DIR = join(process.env.HOME || '/tmp', '.discord-cli', 'audio');
mkdirSync(AUDIO_DIR, { recursive: true });
export const TMUX_FIELD_SEP = '__TOKEN_DISCORD_FIELD__';

/**
 * Split tmux output fields using the daemon-owned separator.
 * Null or undefined input is treated as an empty string and returns [''].
 *
 * @param {string | null | undefined} line
 * @returns {string[]}
 */
export function parseTmuxFields(line) {
  return String(line || '').split(TMUX_FIELD_SEP);
}

function normalizeBotName(botName) {
  return String(botName || 'unknown').trim().toLowerCase().replaceAll('-', '_');
}

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


  function tmuxExecOptions(extra = {}) {
    // The daemon runs inside its own tmux pane. If TMUX is inherited, tmux
    // client-scoped queries such as `display-message -c /dev/ttys000` can be
    // evaluated against the daemon's pane instead of the human client. Route
    // discovery must query the server as an external client.
    const { TMUX, ...env } = process.env;
    return { ...extra, env };
  }

  function paneInfo(pane) {
    if (!pane?.startsWith?.('%')) return null;
    try {
      const raw = execFileSync('tmux', [
        'display-message',
        '-t',
        pane,
        '-p',
        `#{pane_id}${TMUX_FIELD_SEP}#{session_name}${TMUX_FIELD_SEP}#{pane_current_command}${TMUX_FIELD_SEP}#{pane_current_path}`,
      ], tmuxExecOptions({ encoding: 'utf8', timeout: 5000 })).trim();
      const [paneId, sessionName, command, currentPath] = parseTmuxFields(raw);
      if (paneId !== pane) return null;
      return { paneId, sessionName, command, currentPath };
    } catch {
      return null;
    }
  }

  function isRoutablePane(pane) {
    const info = paneInfo(pane);
    if (!info) return false;
    if (info.sessionName === 'discord-daemon') return false;
    if (info.sessionName?.startsWith?.('tx_test_')) return false;
    if ((info.currentPath || '').endsWith('/Token-OS/discord-daemon')) return false;
    return true;
  }

  function resolveFallbackTmuxPane() {
    try {
      const windowsOut = execFileSync('tmux', [
        'list-windows',
        '-a',
        '-F',
        `#{session_name}${TMUX_FIELD_SEP}#{window_active}${TMUX_FIELD_SEP}#{window_index}${TMUX_FIELD_SEP}#{pane_id}`,
      ], tmuxExecOptions({ encoding: 'utf8', timeout: 5000 }));

      const candidates = [];
      for (const line of windowsOut.split(/\r?\n/)) {
        if (!line) continue;
        const [sessionName, rawActive, rawIndex, pane] = parseTmuxFields(line);
        if (!pane?.startsWith?.('%')) continue;
        if (sessionName === 'discord-daemon' || sessionName?.startsWith?.('tx_test_')) continue;
        if (!isRoutablePane(pane)) continue;
        const active = rawActive === '1' ? 1 : 0;
        const index = Number.parseInt(rawIndex || '9999', 10);
        candidates.push({ sessionName, pane, active, index: Number.isFinite(index) ? index : 9999 });
      }

      candidates.sort((a, b) => {
        if (a.sessionName === 'main' && b.sessionName !== 'main') return -1;
        if (b.sessionName === 'main' && a.sessionName !== 'main') return 1;
        if (b.active !== a.active) return b.active - a.active;
        return a.index - b.index;
      });

      if (candidates.length > 0) {
        const chosen = candidates[0];
        logger.warn(`Voice: falling back to active ${chosen.sessionName} pane ${chosen.pane}`);
        return chosen.pane;
      }
    } catch (err) {
      logger.warn(`Voice: fallback tmux pane resolve failed: ${err.message}`);
    }
    return null;
  }

  function resolveSelectedTmuxPane() {
    try {
      const clientsOut = execFileSync('tmux', [
        'list-clients',
        '-F',
        `#{client_activity}${TMUX_FIELD_SEP}#{client_name}${TMUX_FIELD_SEP}#{session_name}`,
      ], tmuxExecOptions({ encoding: 'utf8', timeout: 5000 }));

      const clients = [];
      for (const line of clientsOut.split(/\r?\n/)) {
        if (!line) continue;
        const [rawActivity, clientName, sessionName] = parseTmuxFields(line);
        const activity = Number.parseInt(rawActivity || '0', 10);
        if (clientName && Number.isFinite(activity) && sessionName !== 'discord-daemon') {
          clients.push({ clientName, sessionName, activity });
        }
      }
      clients.sort((a, b) => b.activity - a.activity);

      for (const client of clients) {
        const pane = execFileSync('tmux', [
          'display-message',
          '-c',
          client.clientName,
          '-p',
          '#{pane_id}',
        ], tmuxExecOptions({ encoding: 'utf8', timeout: 5000 })).trim();
        if (isRoutablePane(pane)) {
          logger.info(`Voice: selected tmux client ${client.clientName} (${client.sessionName}) pane ${pane}`);
          return pane;
        }
        logger.warn(`Voice: selected pane ${pane || '?'} from client ${client.clientName} is not routable`);
      }

      logger.warn('Voice: no routable attached tmux client pane found');
      return resolveFallbackTmuxPane();
    } catch (err) {
      logger.warn(`Voice: selected tmux pane resolve failed: ${err.message}`);
      return resolveFallbackTmuxPane();
    }
  }

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
    const silenceFilter = new Transform({
      transform(chunk, encoding, callback) {
        // Discord silence frames are ≤5 bytes (typically 3 bytes: 0xF8 0xFF 0xFE).
        // Speech frames are 40-80+ bytes. Filter silence to prevent Opus decoder corruption.
        if (chunk.length <= 5) {
          silenceFilter.emit('silence');
          callback();
        } else {
          callback(null, chunk);
        }
      }
    });

    // Decode Opus → PCM (48kHz mono s16le)
    const decoder = new prism.opus.Decoder({
      rate: 48000,
      channels: 1, // Mono for transcription
      frameSize: 960,
    });

    let hasAudioSinceCommit = false;
    let bytesSinceCommit = 0;
    let silenceTimer = null;
    let lockedTmuxPane = null;
    let suppressEndClose = false;

    function currentRouteMeta(extra = {}) {
      return {
        routeEpoch: state.routeEpoch,
        channelId: state.channelId,
        lockedTmuxPane,
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
      if (!hasAudioSinceCommit) return false;
      const audioMs = downsampledDurationMs(bytesSinceCommit);
      if (audioMs < MIN_LOCAL_COMMIT_AUDIO_MS) {
        logger.debug?.(
          `Voice [${botName}]: skipping tiny realtime audio commit from ${userId} ` +
          `(${Math.round(audioMs)}ms, ${bytesSinceCommit} bytes, reason=${reason})`
        );
        hasAudioSinceCommit = false;
        bytesSinceCommit = 0;
        lockedTmuxPane = null;
        return false;
      }
      logger.info(`Voice [${botName}]: committing realtime audio from ${userId} (${bytesSinceCommit} bytes, reason=${reason})`);
      if (onAudioCommit) {
        try { onAudioCommit(userId, botName, currentRouteMeta({ reason, audioMs, ...extra })); } catch {}
      }
      hasAudioSinceCommit = false;
      bytesSinceCommit = 0;
      lockedTmuxPane = null;
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
      lockedTmuxPane = null;
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

    decoder.on('data', (chunk) => {
      // First real audio frame of a local utterance: only Cadia/Imperial Guard
      // owns active-pane locks. Static persona bots must never receive or fall
      // back to this Cadia pane.
      if (!hasAudioSinceCommit) {
        if (normalizeBotName(botName) === 'imperial_guard') {
          lockedTmuxPane = resolveSelectedTmuxPane();
          if (lockedTmuxPane) {
            logger.info(`Voice [${botName}]: locked selected tmux pane ${lockedTmuxPane} for user ${userId}`);
          } else {
            logger.warn(`Voice [${botName}]: no selected tmux pane lock for user ${userId}`);
          }
        } else {
          lockedTmuxPane = null;
        }
      }

      if (onAudioFrame) {
        try { onAudioFrame(userId, chunk, botName, currentRouteMeta({ silence: false })); } catch {}
      }
      // Real audio arrived — cancel any pending silence commit.
      if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }

      hasAudioSinceCommit = true;
      bytesSinceCommit += chunk.length;
    });

    audioStream.pipe(silenceFilter).pipe(decoder);

    // Stream only ends on manual destroy (leave/stop) — we handle that in leaveChannel
    decoder.on('end', () => {
      if (silenceTimer) clearTimeout(silenceTimer);
      state.activeSubscriptions.delete(userId);
      commitPending('stream-end');
      if (!suppressEndClose && onAudioEnd) {
        try { onAudioEnd(userId, botName); } catch {}
      }
    });

    decoder.on('error', (err) => {
      if (silenceTimer) clearTimeout(silenceTimer);
      logger.error(`Voice [${botName}]: decoder error for ${userId}: ${err.message}`);
      state.activeSubscriptions.delete(userId);
      if (!suppressEndClose && onAudioEnd) {
        try { onAudioEnd(userId, botName); } catch {}
      }
    });

    audioStream.on('error', (err) => {
      if (silenceTimer) clearTimeout(silenceTimer);
      logger.error(`Voice [${botName}]: stream error for ${userId}: ${err.message}`);
      state.activeSubscriptions.delete(userId);
      if (!suppressEndClose && onAudioEnd) {
        try { onAudioEnd(userId, botName); } catch {}
      }
    });

    state.activeSubscriptions.set(userId, { stream: audioStream, decoder, commit: commitPending, discard: discardPending });
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
   */
  async function playAudio(filePath, botName = 'mechanicus') {
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
    muteMember,
    unmuteMember,
    setAudioFrameCallback(cb) { onAudioFrame = cb; },
    setAudioEndCallback(cb) { onAudioEnd = cb; },
    setAudioCommitCallback(cb) { onAudioCommit = cb; },
    setVoiceLeaveCallback(cb) { onVoiceLeave = cb; },
    reconcileOperatorVoiceState,
  };
}
