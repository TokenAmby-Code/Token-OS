#!/usr/bin/env node
// daemon.js — Discord CLI daemon entry point
// Maintains WebSocket connection, exposes local HTTP API

import { createDiscordClient, createBotClients, loadConfig } from './discord-client.js';
import { createHttpServer } from './http-server.js';
import { createMessageStore } from './message-store.js';
import { createVoiceManager } from './voice.js';
import { createTranscriber } from './transcribe.js';
import { writeFileSync, unlinkSync, mkdirSync, appendFileSync, readFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';
import { SlashCommandBuilder, Events } from 'discord.js';
import { execFile, execFileSync } from 'child_process';

const __dirname = dirname(fileURLToPath(import.meta.url));
const BASE_DIR = join(__dirname, '..');
const LOCAL_DIR = join(process.env.HOME || '/tmp', '.discord-cli');
const PID_FILE = join(LOCAL_DIR, 'daemon.pid');
const LOG_DIR = join(LOCAL_DIR, 'logs');

mkdirSync(LOG_DIR, { recursive: true });

// --- Logger ---
const LOG_LEVELS = { debug: 0, info: 1, warn: 2, error: 3 };
// FG config knob: a named child label to redirect Discord error logs to, so FG
// is not hounded by a child's own active work. Empty = route to FG (default).
// The actual target is resolved against LIVE instances by token-api at fire
// time — no stale @DISCORD_VOICE_FIXER_INSTANCE_ID / _PANE markers are trusted.
const FIXER_REDIRECT_ENV = 'DISCORD_VOICE_FIXER_REDIRECT';
const FIXER_REDIRECT_TMUX_OPTION = '@DISCORD_VOICE_FIXER_REDIRECT';

function tmuxExecOptions(extra = {}) {
  const { TMUX, ...env } = process.env;
  return { ...extra, env };
}

function classifyErrorCode(msg, meta = {}) {
  if (meta.errorCode) return meta.errorCode;
  const text = String(msg || '').toLowerCase();
  if (text.includes('input audio buffer') || text.includes('buffer too small')) return 'realtime_input_audio_buffer_commit';
  if (text.includes('/voice/tts')) return 'voice_tts_route_error';
  if (text.includes('cannot perform ip discovery')) return 'discord_voice_ip_discovery';
  if (text.includes('not connected to a voice channel')) return 'voice_bot_not_connected';
  if (text.includes('websocket')) return 'realtime_websocket_error';
  if (text.includes('unhandled rejection')) return 'unhandled_rejection';
  if (text.includes('uncaught exception')) return 'uncaught_exception';
  return 'discord_daemon_error';
}

function tailFile(path, maxLines = 80) {
  try {
    const data = readFileSync(path, 'utf8');
    return data.split(/\r?\n/).filter(Boolean).slice(-maxLines).join('\n');
  } catch {
    return '';
  }
}

function createDiscordFixerHook() {
  const recent = new Map();
  const RECENT_TTL_MS = 10 * 60_000;
  const RECENT_MAX = 500;
  const agentCmd = join(BASE_DIR, 'cli-tools', 'bin', 'agent-cmd');

  let cachedPort = null;
  function tokenApiPort() {
    if (cachedPort) return cachedPort;
    try { cachedPort = loadConfig().token_api_port; } catch {}
    cachedPort = cachedPort || 7777;
    return cachedPort;
  }

  function resolveFixerRedirect() {
    // FG's config knob: the named child label to receive Discord error logs.
    const envValue = process.env[FIXER_REDIRECT_ENV];
    if (envValue) return envValue.trim();
    try {
      return execFileSync('tmux', ['show-options', '-gqv', FIXER_REDIRECT_TMUX_OPTION], tmuxExecOptions({
        encoding: 'utf8',
        timeout: 3000,
      })).trim();
    } catch {
      return '';
    }
  }

  async function resolveLiveFixerTarget() {
    // Resolve the routing target against LIVE instances at fire time: FG by
    // default, or FG's named child when the redirect knob is set and live.
    // Returns {instance_id, tmux_pane, label, source} or null — never a stale
    // session-stub or a dead fallback pane.
    const redirect = resolveFixerRedirect();
    const qs = redirect ? `?redirect=${encodeURIComponent(redirect)}` : '';
    try {
      const resp = await fetch(`http://127.0.0.1:${tokenApiPort()}/api/discord/fixer-target${qs}`, {
        method: 'GET',
        signal: AbortSignal.timeout(4000),
      });
      if (!resp.ok) return null;
      const data = await resp.json();
      return data?.target || null;
    } catch {
      return null;
    }
  }

  function pasteFixerPromptToPane(pane, prompt, logFile) {
    const bufferName = `discord-fixer-hook-${process.pid}`;
    try {
      execFileSync('tmux', ['set-buffer', '-b', bufferName, prompt], tmuxExecOptions({ timeout: 5000 }));
      execFileSync('tmux', ['paste-buffer', '-d', '-b', bufferName, '-t', pane], tmuxExecOptions({ timeout: 5000 }));
      execFileSync('tmux', ['send-keys', '-t', pane, 'Enter'], tmuxExecOptions({ timeout: 5000 }));
    } catch (err) {
      const failLine = `${new Date().toISOString()} [WARN ] Discord fixer hook raw tmux paste failed pane=${pane}: ${err.message}`;
      console.log(failLine);
      try { appendFileSync(logFile, failLine + '\n'); } catch {}
    }
  }

  function sendFixerPrompt(args, prompt, logFile, fallback = null) {
    execFile(agentCmd, [...args, prompt], {
      env: {
        ...process.env,
        PATH: [
          join(BASE_DIR, 'cli-tools', 'bin'),
          '/opt/homebrew/bin',
          '/usr/local/bin',
          process.env.PATH || '',
        ].join(':'),
      },
      timeout: 45_000,
      maxBuffer: 1024 * 1024,
    }, (err, stdout, stderr) => {
      if (!err) return;
      if (fallback) {
        fallback(`${err.message}; ${String(stderr || stdout || '').slice(0, 240)}`);
        return;
      }
      const failLine = `${new Date().toISOString()} [WARN ] Discord fixer hook failed: ${err.message}; ${String(stderr || stdout || '').slice(0, 240)}`;
      console.log(failLine);
      try { appendFileSync(logFile, failLine + '\n'); } catch {}
    });
  }

  return async function hookDiscordError(line, msg, meta = {}, logFile) {
    const errorCode = classifyErrorCode(msg, meta);
    const key = `${errorCode}:${String(msg).slice(0, 180)}`;
    const now = Date.now();
    for (const [k, ts] of recent) {
      if (now - ts > RECENT_TTL_MS) recent.delete(k);
    }
    if (recent.size > RECENT_MAX) {
      const oldestKey = recent.keys().next().value;
      if (oldestKey) recent.delete(oldestKey);
    }
    const last = recent.get(key) || 0;
    if (now - last < 30_000) return;
    recent.set(key, now);

    // Resolve the routing target against LIVE instances at fire time: FG by
    // default, or FG's named child via the redirect knob. If nothing live
    // resolves, DROP — never dead-letter into a stale stub or someone else's
    // pane (the bug that repeatedly spammed the Custodes pane).
    let target = null;
    try {
      target = await resolveLiveFixerTarget();
    } catch {
      target = null;
    }
    if (!target || !target.instance_id) {
      const failLine = `${new Date().toISOString()} [WARN ] Discord fixer hook: no LIVE fixer target (FG offline / redirect dead); dropping ${errorCode}`;
      console.log(failLine);
      try { appendFileSync(logFile, failLine + '\n'); } catch {}
      return;
    }

    const recentLog = tailFile(logFile, 80);
    const prompt = [
      'Discord voice routing state hook to active fixer.',
      '',
      `error_code: ${errorCode}`,
      `fixer_instance_id: ${target.instance_id}`,
      `fixer_label: ${target.label || ''}`,
      `fixer_source: ${target.source || ''}`,
      `timestamp: ${new Date(now).toISOString()}`,
      `trigger: ${msg}`,
      '',
      'Recent discord-daemon log:',
      '```text',
      recentLog || line,
      '```',
    ].join('\n');

    sendFixerPrompt(['--instance', target.instance_id], prompt, logFile, (instanceError) => {
      // Live-pane fallback: only the pane the resolver verified for THIS target
      // (never a hardcoded/stale pane). Skip the paste if it is not a real pane.
      const fixerPane = target.tmux_pane;
      if (!fixerPane || !fixerPane.startsWith('%')) {
        const failLine = `${new Date().toISOString()} [WARN ] Discord fixer hook failed: ${instanceError}; target ${target.instance_id} has no live pane`;
        console.log(failLine);
        try { appendFileSync(logFile, failLine + '\n'); } catch {}
        return;
      }
      const fallbackPrompt = `${prompt}\n\n(instance delivery failed, live-pane fallback used: ${instanceError})`;
      pasteFixerPromptToPane(fixerPane, fallbackPrompt, logFile);
    });
  };
}

const discordFixerHook = createDiscordFixerHook();

function createLogger(level = 'info') {
  const minLevel = LOG_LEVELS[level] ?? 1;
  const logFile = join(LOG_DIR, `daemon-${new Date().toISOString().slice(0, 10)}.log`);

  function log(lvl, msg, meta = {}) {
    if (LOG_LEVELS[lvl] < minLevel) return;
    const line = `${new Date().toISOString()} [${lvl.toUpperCase().padEnd(5)}] ${msg}`;
    console.log(line);
    try {
      appendFileSync(logFile, line + '\n');
    } catch {
      // Don't crash on log write failure
    }
    if (lvl === 'error') {
      try { Promise.resolve(discordFixerHook(line, msg, meta, logFile)).catch(() => {}); } catch {}
    }
  }

  return {
    debug: (msg, meta) => log('debug', msg, meta),
    info: (msg, meta) => log('info', msg, meta),
    warn: (msg, meta) => log('warn', msg, meta),
    error: (msg, meta) => log('error', msg, meta),
  };
}

const earlyLogger = createLogger('debug');
process.on('beforeExit', (code) => {
  earlyLogger.warn(`Process beforeExit code=${code}`);
});
process.on('exit', (code) => {
  earlyLogger.warn(`Process exit code=${code}`);
});
process.on('uncaughtException', (err) => {
  earlyLogger.error(`Uncaught exception: ${err.stack || err.message}`);
  process.exitCode = 1;
});
process.on('unhandledRejection', (reason) => {
  const message = reason?.stack || reason?.message || String(reason);
  earlyLogger.error(`Unhandled rejection: ${message}`);
  process.exitCode = 1;
});
process.on('SIGHUP', () => {
  earlyLogger.warn('Received SIGHUP');
  process.exit(0);
});

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

  // Wire live decoded Discord PCM frames into OpenAI Realtime transcription.
  voiceManager.setAudioFrameCallback((userId, pcmChunk, botName, meta) => {
    return transcriber.handleAudioFrame?.(userId, pcmChunk, botName, meta);
  });
  voiceManager.setAudioEndCallback((userId, botName) => {
    return transcriber.closeUser?.(userId, botName);
  });
  voiceManager.setAudioCommitCallback((userId, botName, meta) => {
    return transcriber.commitUser?.(userId, botName, meta);
  });

  // Forward transcription results to Token API
  transcriber.onTranscription(async (result) => {
    const botLabel = result.botName || 'voice';
    logger.info(`Transcription [${botLabel}] from ${result.userId}: "${result.text}"`);
    try {
      const resp = await fetch(`http://127.0.0.1:${config.token_api_port}/api/discord/message`, {
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
          target_tmux_pane: result.lockedTmuxPane || result.commitMeta?.lockedTmuxPane || null,
          voice_no_submit: !!result.noSubmit,
          voice_append_submit: !!result.appendSubmit,
        }),
      });
      logger.info(`Transcription [${botLabel}]: Token API ack ${resp.status}`);
      if (!resp.ok) {
        logger.warn(`Transcription [${botLabel}]: Token API response ${await resp.text()}`);
      }
    } catch (err) {
      logger.warn(`Transcription [${botLabel}]: Token API forward failed: ${err.message}`);
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
    try {
      await client.start();
      logger.info(`Bot '${name}' connected`);
    } catch (err) {
      logger.warn(`Bot '${name}' failed to connect: ${err.message}`);
      delete botClients[name];
    }
  }

  await voiceManager.reconcileOperatorVoiceState();

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
    if (err?.code === 'EADDRINUSE') {
      process.exit(1);
    }
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
