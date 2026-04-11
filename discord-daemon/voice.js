// voice.js — Discord voice channel management
// Handles joining/leaving voice channels, audio capture, and transcription pipeline
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
import { createWriteStream, mkdirSync } from 'fs';
import { join } from 'path';
import { pipeline } from 'stream/promises';
import { Transform } from 'stream';
import { Events } from 'discord.js';
import prism from 'prism-media';

const AUDIO_DIR = join(process.env.HOME || '/tmp', '.discord-cli', 'audio');
mkdirSync(AUDIO_DIR, { recursive: true });

export function createVoiceManager(botClients, config, logger) {
  const guildId = config.guild_id;
  const operatorUserId = config.operator_user_id;
  const voiceChannels = config.voice_channels || {};

  // Per-bot connection state: botName -> { connection, recording, subscriptions, channelId }
  const botStates = new Map();

  // Collect all bot user IDs to filter out of audio capture (prevent ouroboros)
  // Populated lazily after bots connect
  const botUserIds = new Set();

  function refreshBotUserIds() {
    for (const client of Object.values(botClients)) {
      const id = client.botUserId || client.client?.user?.id;
      if (id) botUserIds.add(id);
    }
  }

  // Transcription callback — set externally
  let onTranscription = null;

  function getBotState(botName) {
    if (!botStates.has(botName)) {
      botStates.set(botName, {
        connection: null,
        recording: false,
        activeSubscriptions: new Map(),
        channelId: null,
      });
    }
    return botStates.get(botName);
  }

  function getClient(botName = 'mechanicus') {
    return botClients[botName] || Object.values(botClients)[0];
  }

  async function joinChannel(voiceChannelId, botName = 'mechanicus') {
    const client = getClient(botName);
    if (!client?.client) throw new Error(`Bot '${botName}' not available`);

    const state = getBotState(botName);
    const guild = await client.client.guilds.fetch(guildId);
    const channel = await guild.channels.fetch(voiceChannelId);

    if (!channel?.isVoiceBased?.()) {
      throw new Error(`Channel ${voiceChannelId} is not a voice channel`);
    }

    // Destroy existing connection if any
    if (state.connection) {
      state.connection.destroy();
    }

    state.connection = joinVoiceChannel({
      channelId: voiceChannelId,
      guildId: guildId,
      adapterCreator: guild.voiceAdapterCreator,
      selfDeaf: false, // MUST be false to receive audio
      selfMute: true,  // Muted by default (no playback yet)
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
      if (!state.recording) return;
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
  }

  // Chunking config
  const MAX_CHUNK_SECONDS = 15;
  const SILENCE_FLUSH_MS = 1500; // Flush after 1.5s of silence (raised from 0.8s — was cutting sentences)
  const BYTES_PER_SECOND = 48000 * 2; // 48kHz mono s16le
  const MAX_CHUNK_BYTES = MAX_CHUNK_SECONDS * BYTES_PER_SECOND;

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

    let chunks = [];
    let totalBytes = 0;
    let chunkIndex = 0;
    let silenceTimer = null;

    function flushChunk() {
      const bytes = totalBytes;
      const buffer = Buffer.concat(chunks);
      chunks = [];
      totalBytes = 0;
      chunkIndex++;

      if (bytes < 3200) {
        logger.debug(`Voice [${botName}]: discarding tiny chunk from ${userId} (${bytes} bytes)`);
        return;
      }
      logger.info(`Voice [${botName}]: captured chunk #${chunkIndex} — ${bytes} bytes from user ${userId}`);
      processAudio(botName, userId, buffer, bytes);
    }

    function startSilenceTimer() {
      if (silenceTimer) clearTimeout(silenceTimer);
      silenceTimer = setTimeout(() => {
        if (totalBytes > 0) {
          logger.info(`Voice [${botName}]: silence detected (${SILENCE_FLUSH_MS}ms), flushing chunk`);
          flushChunk();
        }
      }, SILENCE_FLUSH_MS);
    }

    // Silence frames from Discord trigger the flush timer
    silenceFilter.on('silence', () => {
      if (totalBytes > 0) {
        startSilenceTimer();
      }
    });

    decoder.on('data', (chunk) => {
      // Real audio arrived — cancel any pending silence flush
      if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }

      chunks.push(chunk);
      totalBytes += chunk.length;

      // Force split on long continuous speech
      if (totalBytes >= MAX_CHUNK_BYTES) {
        logger.info(`Voice [${botName}]: max chunk reached (${MAX_CHUNK_SECONDS}s), flushing...`);
        flushChunk();
      }
    });

    audioStream.pipe(silenceFilter).pipe(decoder);

    // Stream only ends on manual destroy (leave/stop) — we handle that in leaveChannel
    decoder.on('end', () => {
      if (silenceTimer) clearTimeout(silenceTimer);
      state.activeSubscriptions.delete(userId);
      if (totalBytes > 0) {
        flushChunk();
      }
    });

    decoder.on('error', (err) => {
      if (silenceTimer) clearTimeout(silenceTimer);
      logger.error(`Voice [${botName}]: decoder error for ${userId}: ${err.message}`);
      state.activeSubscriptions.delete(userId);
    });

    audioStream.on('error', (err) => {
      if (silenceTimer) clearTimeout(silenceTimer);
      logger.error(`Voice [${botName}]: stream error for ${userId}: ${err.message}`);
      state.activeSubscriptions.delete(userId);
    });

    state.activeSubscriptions.set(userId, { stream: audioStream, decoder, flush: flushChunk });
  }

  async function processAudio(botName, userId, pcmBuffer, totalBytes) {
    const timestamp = Date.now();
    const filename = `${userId}-${timestamp}.pcm`;
    const filepath = join(AUDIO_DIR, filename);

    // Save PCM to file for debugging/retry
    const ws = createWriteStream(filepath);
    ws.write(pcmBuffer);
    ws.end();

    logger.info(`Voice [${botName}]: saved audio to ${filepath} (${totalBytes} bytes)`);

    // If transcription callback is set, call it with bot context
    if (onTranscription) {
      try {
        await onTranscription(userId, pcmBuffer, filepath, botName);
      } catch (err) {
        logger.error(`Voice [${botName}]: transcription callback error: ${err.message}`);
      }
    }
  }

  async function leaveChannel(botName = 'mechanicus') {
    const state = getBotState(botName);
    if (!state.connection) return { left: false, reason: 'not connected' };

    // Flush any accumulated audio before destroying subscriptions
    for (const [userId, sub] of state.activeSubscriptions) {
      if (sub.flush) {
        try { sub.flush(); } catch {}
      }
      try { sub.stream.destroy(); } catch {}
      try { sub.decoder.destroy(); } catch {}
    }
    state.activeSubscriptions.clear();

    state.connection.destroy();
    state.connection = null;
    state.recording = false;
    const leftChannel = state.channelId;
    state.channelId = null;

    logger.info(`Voice [${botName}]: left channel ${leftChannel}`);
    return { left: true, channelId: leftChannel, botName };
  }

  function startRecording(botName = 'mechanicus') {
    const state = getBotState(botName);
    if (!state.connection) throw new Error(`Bot '${botName}' not connected to a voice channel`);
    state.recording = true;
    logger.info(`Voice [${botName}]: recording started`);
    return { recording: true, channelId: state.channelId, botName };
  }

  function stopRecording(botName = 'mechanicus') {
    const state = getBotState(botName);
    state.recording = false;
    // Flush and clean up active subscriptions
    for (const [userId, sub] of state.activeSubscriptions) {
      if (sub.flush) {
        try { sub.flush(); } catch {}
      }
      try { sub.stream.destroy(); } catch {}
      try { sub.decoder.destroy(); } catch {}
    }
    state.activeSubscriptions.clear();
    logger.info(`Voice [${botName}]: recording stopped`);
    return { recording: false, channelId: state.channelId, botName };
  }

  function getStatus(botName) {
    // If botName specified, return that bot's status
    if (botName) {
      const state = getBotState(botName);
      return {
        botName,
        connected: !!state.connection,
        channelId: state.channelId,
        recording: state.recording,
        activeListeners: state.activeSubscriptions.size,
        connectionState: state.connection?.state?.status || 'disconnected',
      };
    }
    // Return all bots' status
    const statuses = {};
    for (const [name, state] of botStates) {
      statuses[name] = {
        connected: !!state.connection,
        channelId: state.channelId,
        recording: state.recording,
        activeListeners: state.activeSubscriptions.size,
        connectionState: state.connection?.state?.status || 'disconnected',
      };
    }
    // Include configured but not-yet-connected bots
    for (const name of Object.keys(voiceChannels)) {
      if (!statuses[name]) {
        statuses[name] = {
          connected: false,
          channelId: null,
          recording: false,
          activeListeners: 0,
          connectionState: 'disconnected',
          assignedChannel: voiceChannels[name],
        };
      }
    }
    return statuses;
  }

  /**
   * Set up auto-join/leave for all bots that have voice_channels configured.
   * Each bot watches for the operator joining/leaving its assigned VC.
   */
  function setupAutoJoin() {
    for (const [botName, channelId] of Object.entries(voiceChannels)) {
      const client = getClient(botName);
      if (!client?.client) {
        logger.warn(`Voice auto-join: bot '${botName}' not available, skipping`);
        continue;
      }

      // Need GuildVoiceStates intent to receive voiceStateUpdate
      client.client.on(Events.VoiceStateUpdate, async (oldState, newState) => {
        // Only care about the operator
        if (newState.member?.id !== operatorUserId && oldState.member?.id !== operatorUserId) return;

        const joinedChannel = newState.channelId;
        const leftChannel = oldState.channelId;
        const state = getBotState(botName);

        // Operator joined our assigned channel
        if (joinedChannel === channelId && leftChannel !== channelId) {
          if (state.connection) {
            logger.debug(`Voice auto-join [${botName}]: already connected`);
            return;
          }
          logger.info(`Voice auto-join [${botName}]: operator joined ${channelId}, following...`);
          try {
            await joinChannel(channelId, botName);
            startRecording(botName);
            logger.info(`Voice auto-join [${botName}]: joined and recording`);
          } catch (err) {
            logger.error(`Voice auto-join [${botName}]: failed to join: ${err.message}`);
          }
        }

        // Operator left our assigned channel
        if (leftChannel === channelId && joinedChannel !== channelId) {
          if (!state.connection) return;
          logger.info(`Voice auto-join [${botName}]: operator left ${channelId}, disconnecting...`);
          try {
            await leaveChannel(botName);
          } catch (err) {
            logger.error(`Voice auto-join [${botName}]: failed to leave: ${err.message}`);
          }
        }
      });

      logger.info(`Voice auto-join [${botName}]: watching for operator in channel ${channelId}`);
    }
  }

  return {
    joinChannel,
    leaveChannel,
    startRecording,
    stopRecording,
    getStatus,
    setupAutoJoin,
    setTranscriptionCallback(cb) { onTranscription = cb; },
  };
}
