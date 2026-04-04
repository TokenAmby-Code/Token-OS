#!/usr/bin/env node
// daemon.js — Discord CLI daemon entry point
// Maintains WebSocket connection, exposes local HTTP API

import { createDiscordClient, createBotClients, loadConfig } from './discord-client.js';
import { createHttpServer } from './http-server.js';
import { createMessageStore } from './message-store.js';
import { createVoiceManager } from './voice.js';
import { createTranscriber } from './transcribe.js';
import { writeFileSync, unlinkSync, mkdirSync, appendFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';
import { SlashCommandBuilder, Events } from 'discord.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const BASE_DIR = join(__dirname, '..');
const LOCAL_DIR = join(process.env.HOME || '/tmp', '.discord-cli');
const PID_FILE = join(LOCAL_DIR, 'daemon.pid');
const LOG_DIR = join(LOCAL_DIR, 'logs');

mkdirSync(LOG_DIR, { recursive: true });

// --- Logger ---
const LOG_LEVELS = { debug: 0, info: 1, warn: 2, error: 3 };

function createLogger(level = 'info') {
  const minLevel = LOG_LEVELS[level] ?? 1;
  const logFile = join(LOG_DIR, `daemon-${new Date().toISOString().slice(0, 10)}.log`);

  function log(lvl, msg) {
    if (LOG_LEVELS[lvl] < minLevel) return;
    const line = `${new Date().toISOString()} [${lvl.toUpperCase().padEnd(5)}] ${msg}`;
    console.log(line);
    try {
      appendFileSync(logFile, line + '\n');
    } catch {
      // Don't crash on log write failure
    }
  }

  return {
    debug: (msg) => log('debug', msg),
    info: (msg) => log('info', msg),
    warn: (msg) => log('warn', msg),
    error: (msg) => log('error', msg),
  };
}

// --- Main ---
async function main() {
  const config = loadConfig();
  const logger = createLogger(config.log_level || 'info');

  logger.info('Discord CLI daemon starting...');
  logger.info(`PID: ${process.pid}`);

  // Write PID file
  writeFileSync(PID_FILE, String(process.pid));

  // Create components
  const messageStore = createMessageStore(logger);

  // Create a client for each configured bot; fall back to single mechanicus client
  const botClients = createBotClients(config, logger);
  const discordClient = botClients['mechanicus'] || Object.values(botClients)[0];

  const voiceManager = createVoiceManager(botClients, config, logger);
  const transcriber = createTranscriber(config, logger);

  // Wire transcriber to voice manager (callback now receives botName)
  voiceManager.setTranscriptionCallback((userId, pcmBuffer, filepath, botName) => {
    return transcriber.handleAudio(userId, pcmBuffer, filepath, botName);
  });

  // Forward transcription results to Token API
  transcriber.onTranscription(async (result) => {
    const botLabel = result.botName || 'voice';
    logger.info(`Transcription [${botLabel}] from ${result.userId}: "${result.text}"`);
    try {
      await fetch(`http://127.0.0.1:${config.token_api_port}/api/discord/message`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message_id: `voice-${result.timestamp}`,
          channel_id: `voice-${botLabel}`,
          channel_name: `voice-${botLabel}`,
          guild_id: config.guild_id,
          author: {
            id: result.userId,
            username: 'voice',
            displayName: 'Voice',
            bot: false,
          },
          content: result.text,
          timestamp: new Date(result.timestamp).toISOString(),
          is_dm: false,
          is_reply: false,
          is_voice: true,
          bot_name: botLabel,
        }),
      });
    } catch {
      // Token API might not be running
    }
  });

  // Set up auto-join/leave for bots with assigned voice channels
  voiceManager.setupAutoJoin();

  const httpServer = createHttpServer(botClients, messageStore, config, logger, voiceManager);

  // Forward incoming messages to Token API if configured (main listening bot only)
  if (config.forward_to_token_api) {
    // Dedup set to prevent double-forwarding replayed events on WebSocket resume.
    // Discord replays missed events on reconnect — they fire through onMessage again.
    const seenMessageIds = new Map(); // message_id -> timestamp (ms)
    const DEDUP_TTL_MS = 5 * 60 * 1000; // 5 minutes — longer than any replay window

    function isDuplicate(messageId) {
      const now = Date.now();
      // Evict stale entries to prevent unbounded growth
      for (const [id, ts] of seenMessageIds) {
        if (now - ts > DEDUP_TTL_MS) seenMessageIds.delete(id);
      }
      if (seenMessageIds.has(messageId)) return true;
      seenMessageIds.set(messageId, now);
      return false;
    }

    discordClient.onMessage(async (msg) => {
      // Don't forward bot messages (except fallback channel — webhook relay)
      if (msg.author.bot && msg.channel_name !== "fallback") return;
      // Skip replayed events (WebSocket resume can replay recently-seen messages)
      if (isDuplicate(msg.message_id)) {
        logger.debug(`Skipping duplicate message forward: ${msg.message_id}`);
        return;
      }

      try {
        const resp = await fetch(`http://127.0.0.1:${config.token_api_port}/api/discord/message`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(msg),
        });
        if (!resp.ok) {
          logger.debug(`Token API forward returned ${resp.status}`);
        }
      } catch {
        // Token API might not be running — that's OK, don't spam logs
      }
    });
  }

  // Recover pending messages from crash
  const pending = messageStore.loadPending();
  if (pending.length > 0) {
    logger.info(`Recovering ${pending.length} pending messages...`);
  }

  // Start all bot clients (mechanicus first as it's the listener)
  for (const [name, client] of Object.entries(botClients)) {
    await client.start();
    logger.info(`Bot '${name}' connected`);
  }

  // Register slash commands on Custodes bot (/task, /note)
  const custodes = botClients['custodes'];
  if (custodes) {
    try {
      const commands = [
        new SlashCommandBuilder()
          .setName('task')
          .setDescription('Create a new task in the vault')
          .addStringOption(opt =>
            opt.setName('title').setDescription('Task title').setRequired(true))
          .addStringOption(opt =>
            opt.setName('body').setDescription('Task details').setRequired(false)),
        new SlashCommandBuilder()
          .setName('note')
          .setDescription('Create a new note in the vault')
          .addStringOption(opt =>
            opt.setName('title').setDescription('Note title').setRequired(true))
          .addStringOption(opt =>
            opt.setName('body').setDescription('Note details').setRequired(false)),
      ];

      // Clear any stale global commands, register guild-scoped only
      await custodes.client.application.commands.set([]);
      const guild = await custodes.client.guilds.fetch(config.guild_id);
      await guild.commands.set(commands.map(c => c.toJSON()));
      logger.info('Custodes slash commands registered (guild-scoped): /task, /note');

      // Handle interactions — deterministic, no AI
      custodes.client.on(Events.InteractionCreate, async (interaction) => {
        if (!interaction.isChatInputCommand()) return;

        const { commandName } = interaction;
        if (commandName !== 'task' && commandName !== 'note') return;

        const title = interaction.options.getString('title');
        const body = interaction.options.getString('body') || '';
        const noteType = commandName === 'task' ? 'prescriptive' : 'descriptive';
        const author = interaction.member?.displayName
          || interaction.user?.displayName
          || interaction.user?.username
          || 'Discord';

        await interaction.deferReply();

        try {
          const resp = await fetch(`http://127.0.0.1:${config.token_api_port}/api/inbox/create`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              title,
              type: noteType,
              content: body,
              source: 'discord',
              author,
            }),
          });

          if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            await interaction.editReply(`Failed: ${err.detail || resp.statusText}`);
            return;
          }

          const result = await resp.json();
          await interaction.editReply(`Created ${noteType} note: **${result.title}**`);
          logger.info(`Slash command /${commandName}: created '${result.title}' by ${author}`);
        } catch (err) {
          logger.error(`Slash command /${commandName} failed: ${err.message}`);
          await interaction.editReply(`Failed to create note: ${err.message}`);
        }
      });
    } catch (err) {
      logger.warn(`Failed to register Custodes slash commands: ${err.message}`);
    }
  }

  // Retry pending messages after connection
  const SNOWFLAKE_RE = /^\d{17,19}$/;
  for (const msg of pending) {
    const pendingFile = msg._filename ? join(BASE_DIR, 'pending', msg._filename) : null;
    try {
      const channelId = config.channels[msg.channel] || msg.channelId;
      if (channelId && msg.content && SNOWFLAKE_RE.test(channelId)) {
        await discordClient.sendMessage(channelId, msg.content);
        logger.info(`Recovered pending message to ${msg.channel}`);
      } else if (channelId && !SNOWFLAKE_RE.test(channelId)) {
        logger.warn(`Dropping stale pending message: invalid channel ID "${channelId}" (${msg.channel})`);
      }
      // Remove from pending regardless (stale messages shouldn't block forever)
      if (pendingFile) { try { unlinkSync(pendingFile); } catch {} }
    } catch (err) {
      logger.error(`Failed to recover pending message: ${err.message}`);
      // Still try to remove the file so it doesn't block future startups
      if (pendingFile) { try { unlinkSync(pendingFile); } catch {} }
    }
  }

  // Start HTTP server
  await httpServer.start();

  logger.info('Daemon ready.');

  // --- Graceful shutdown ---
  async function shutdown(signal) {
    logger.info(`Received ${signal}, shutting down...`);
    try {
      await httpServer.stop();
      for (const client of Object.values(botClients)) {
        await client.stop();
      }
      unlinkSync(PID_FILE);
    } catch (err) {
      logger.error(`Shutdown error: ${err.message}`);
    }
    logger.info('Daemon stopped.');
    process.exit(0);
  }

  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));

  // Keep alive
  process.on('uncaughtException', (err) => {
    logger.error(`Uncaught exception: ${err.message}\n${err.stack}`);
    // Don't crash — discord.js should handle reconnection
  });

  process.on('unhandledRejection', (reason) => {
    logger.error(`Unhandled rejection: ${reason}`);
  });
}

main().catch((err) => {
  console.error(`Fatal: ${err.message}`);
  try { unlinkSync(PID_FILE); } catch {}
  process.exit(1);
});
