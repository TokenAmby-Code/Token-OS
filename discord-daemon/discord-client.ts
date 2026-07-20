// discord-client.js — discord.js v14 connection management
// Subscribes to ALL messages in configured channels (not just pings)

import { Client, GatewayIntentBits, Partials, Events } from 'discord.js';
import { readFileSync, writeFileSync, existsSync, chmodSync, realpathSync } from 'fs';
import { execFileSync } from 'child_process';
import { join, dirname, resolve } from 'path';
import { fileURLToPath } from 'url';
import {
  DISCORD_MESSAGE_CONTENT_LIMIT,
  sendChunkedDiscordContent,
} from './outbound-message.ts';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = join(__dirname, '..', 'config.json');
const ENV_PATH = join(process.env.HOME, '.discord-cli', '.env');

// Config resolution is pinned to <checkout>/config.json via import.meta.url.
// Guard the stale-NAS-path resurrection class: the daemon must run from a
// local runtime checkout, never a NAS mount — a NAS-resident config brings
// back the stale-config crashloop
// (Mars/Tasks/discord-daemon-stale-nas-config-path-pin.md). Fail loud, no
// fallback.
const NAS_MOUNT_ROOTS = ['/Volumes/Imperium', '/mnt/imperium'];

export function assertConfigPathSafe(configPath: string, mountRoots: string[] = NAS_MOUNT_ROOTS): string {
  const input = String(configPath || '');
  // Canonicalize before checking: `..` segments and symlinks must not smuggle
  // a NAS-resident config past a lexical prefix test. A missing file falls
  // back to lexical resolution — the subsequent read fails loud on its own.
  let canonical;
  try {
    canonical = realpathSync(input);
  } catch {
    canonical = resolve(input);
  }
  if (mountRoots.some((root) => canonical === root || canonical.startsWith(`${root}/`))) {
    throw new Error(
      `discord-daemon config path resolved onto the NAS (${canonical}); ` +
      'run the daemon from a local runtime checkout',
    );
  }
  return canonical;
}

// Load .env file into a map (does not pollute process.env)
function loadEnvFile() {
  if (!existsSync(ENV_PATH)) return {};
  const vars = {};
  for (const line of readFileSync(ENV_PATH, 'utf-8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq > 0) vars[trimmed.slice(0, eq)] = trimmed.slice(eq + 1);
  }
  return vars;
}

const envTokens = loadEnvFile();

export function loadConfig() {
  return JSON.parse(readFileSync(assertConfigPathSafe(CONFIG_PATH), 'utf-8'));
}

// Map keychain service names to .env variable names
const KEYCHAIN_TO_ENV = {
  'discord-bot-token': 'DISCORD_BOT_TOKEN',
  'discord-bot-token-custodes': 'DISCORD_BOT_TOKEN_CUSTODES',
  'discord-bot-token-inquisition': 'DISCORD_BOT_TOKEN_INQUISITION',
  'discord-bot-token-imperial-guard': 'DISCORD_BOT_TOKEN_IMPERIAL_GUARD',
};

// Token resolution priority: keychain (canonical, config.token_source) first,
// then the .env cache, then the fallback JSON file. The .env file is ONLY a
// cache for launchd boots where the keychain is locked/unavailable — it must
// never shadow a live keychain read, or a rotated keychain token can never
// take effect while a stale cache exists (that wedge kept dead tokens in use
// after the 2026-07-17 fleet-wide token invalidation).
/**
 * @returns {{token: string, source: 'keychain'|'env'|'fallback_file', keychainService: string|undefined}}
 */
export function resolveBotToken(config, botConfig, { readKeychainToken, envTokens }) {
  const keychainService = botConfig?.keychain_service
    || config.token_keychain_service
    || config.bots?.mechanicus?.keychain_service;

  // Priority 1: macOS keychain — canonical source
  if (config.token_source === 'keychain' && keychainService) {
    try {
      const token = String(readKeychainToken(keychainService) || '').trim();
      if (token) return { token, source: 'keychain', keychainService };
    } catch {
      // Keychain locked/unavailable (headless launchd boot) — fall through
    }
  }

  // Priority 2: .env cache (survives a locked keychain under launchd)
  if (keychainService) {
    const envKey = KEYCHAIN_TO_ENV[keychainService];
    if (envKey && envTokens[envKey]) {
      return { token: envTokens[envKey], source: 'env', keychainService };
    }
  }

  // Priority 3: fallback JSON file
  if (config.token_fallback_file && config.token_fallback_path) {
    try {
      const filePath = config.token_fallback_file.replace('~', process.env.HOME);
      const data = JSON.parse(readFileSync(filePath, 'utf-8'));
      const keys = config.token_fallback_path.split('.');
      let val = data;
      for (const k of keys) val = val[k];
      if (val) return { token: val, source: 'fallback_file', keychainService };
    } catch {
      // Fall through
    }
  }

  throw new Error(`No Discord bot token found for bot${botConfig ? ` (${botConfig.keychain_service})` : ''}`);
}

function readKeychainToken(service) {
  // Argument array, not shell interpolation: the service name must never be
  // parsed by a shell.
  return execFileSync(
    'security', ['find-generic-password', '-s', service, '-w'],
    { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] }
  ).trim();
}

// Keep the launchd .env cache in sync with the keychain, preserving entries
// the keychain does not hold (e.g. a bot whose token only lives in .env).
function refreshEnvCache(keychainService, token) {
  const envKey = KEYCHAIN_TO_ENV[keychainService];
  if (!envKey || envTokens[envKey] === token) return;
  envTokens[envKey] = token;
  try {
    const lines = Object.entries(envTokens).map(([k, v]) => `${k}=${v}`);
    writeFileSync(ENV_PATH, lines.join('\n') + '\n', { encoding: 'utf-8', mode: 0o600 });
  } catch { /* non-fatal — .env is a convenience cache */ }
}

function getToken(config, botConfig = null) {
  const resolved = resolveBotToken(config, botConfig, { readKeychainToken, envTokens });
  if (resolved.source === 'keychain') refreshEnvCache(resolved.keychainService, resolved.token);
  return resolved.token;
}

/**
 * @returns {object} client handle: { start, stop, sendMessage, getStatus, ... }
 */
export function createDiscordClient(config, logger, botName = 'mechanicus', botConfig = null) {
  const resolvedBotConfig = botConfig || config.bots?.[botName] || null;
  const token = getToken(config, resolvedBotConfig);

  // Build reverse channel map: ID -> name
  const channelIdToName = {};
  for (const [name, id] of Object.entries(config.channels)) {
    channelIdToName[id] = name;
  }
  const allowedChannelIds = new Set(Object.values(config.channels));

  // The default (listener) bot needs full intents to receive messages + reactions.
  // Send-only bots (custodes, inquisition) only need Guilds to send to channels.
  const isListener = resolvedBotConfig?.default === true || botName === 'mechanicus';

  // Bots with assigned voice channels need GuildVoiceStates for auto-join/leave
  const hasVoiceChannel = !!(config.voice_channels?.[botName]);

  const client = new Client(isListener ? {
    // Full intents: read ALL message content, DMs, reactions, voice states
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMessages,
      GatewayIntentBits.MessageContent,
      GatewayIntentBits.DirectMessages,
      GatewayIntentBits.GuildMessageReactions,
      GatewayIntentBits.GuildVoiceStates,
    ],
    partials: [
      Partials.Channel,
      Partials.Message,
      Partials.Reaction,
    ],
  } : {
    // Non-listener bots: Guilds + GuildVoiceStates if they have a voice channel
    intents: hasVoiceChannel
      ? [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates]
      : [GatewayIntentBits.Guilds],
  });

  // Event: ready
  client.once(Events.ClientReady, (c) => {
    logger.info(`Connected as ${c.user.tag} | Guild: ${config.guild_id}`);
    logger.info(`Listening on ${allowedChannelIds.size} channels + operator DMs`);
  });

  // Message handlers registry
  const messageHandlers = [];
  const reactionHandlers = [];

  // Event: ALL messages in allowed channels + operator DMs
  client.on(Events.MessageCreate, async (message) => {
    // Skip bot's own messages
    if (message.author.id === client.user.id) return;

    const isDM = !message.guild;
    const isAllowedChannel = allowedChannelIds.has(message.channelId);
    const isOperatorDM = isDM && message.author.id === config.operator_user_id;

    // Thread support: if message is in a thread whose parent is an allowed channel, forward it
    const isThread = message.channel.isThread?.() || false;
    const parentChannelId = isThread ? message.channel.parentId : null;
    const isAllowedThread = isThread && allowedChannelIds.has(parentChannelId);

    if (!isAllowedChannel && !isOperatorDM && !isAllowedThread) return;

    const channelName = isDM ? 'dm'
      : isAllowedThread ? (channelIdToName[parentChannelId] || parentChannelId)
      : (channelIdToName[message.channelId] || message.channelId);

    const msgData = {
      message_id: message.id,
      channel_id: message.channelId,
      channel_name: channelName,
      guild_id: message.guild?.id || null,
      author: {
        id: message.author.id,
        username: message.author.username,
        displayName: message.member?.displayName || message.author.displayName || message.author.username,
        bot: message.author.bot,
      },
      content: message.content,
      timestamp: message.createdAt.toISOString(),
      is_dm: isDM,
      is_reply: !!message.reference,
      reply_to_message_id: message.reference?.messageId || null,
      attachments: message.attachments.map(a => ({ url: a.url, name: a.name })),
      embeds: message.embeds.length,
      // Thread metadata
      is_thread: isThread,
      thread_id: isThread ? message.channelId : null,
      thread_name: isThread ? (message.channel.name || null) : null,
      parent_channel_id: parentChannelId,
      parent_channel_name: isAllowedThread ? channelName : null,
    };

    logger.debug(`[${channelName}] ${msgData.author.username}: ${message.content.slice(0, 80)}`);

    // Direct forward for fallback channel (webhook messages bypass mechanicus handler)
    if (channelName === 'fallback') {
      try {
        const resp = await fetch(`http://127.0.0.1:${config.token_api_port || 7777}/api/discord/message`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(msgData),
        });
        logger.info(`Fallback forwarded to Token API: ${resp.status}`);
      } catch (err) {
        logger.warn(`Fallback forward failed: ${err.message}`);
      }
    }

    // Notify all registered handlers
    for (const handler of messageHandlers) {
      try {
        handler(msgData);
      } catch (err) {
        logger.error(`Message handler error: ${err.message}`);
      }
    }
  });

  // Event: reactions (for /wait endpoint)
  client.on(Events.MessageReactionAdd, async (reaction, user) => {
    if (user.id === client.user?.id) return;

    // Fetch partial if needed
    if (reaction.partial) {
      try { await reaction.fetch(); } catch { return; }
    }

    const reactionData = {
      message_id: reaction.message.id,
      channel_id: reaction.message.channelId,
      emoji: reaction.emoji.name,
      user_id: user.id,
      username: user.username,
    };

    for (const handler of reactionHandlers) {
      try {
        handler(reactionData);
      } catch (err) {
        logger.error(`Reaction handler error: ${err.message}`);
      }
    }
  });

  // Reconnection handling — discord.js v14 handles this automatically
  // but we log the events. Every line carries the bot name: untagged
  // "Disconnected (code 4004)" lines made the 2026-07-17 token invalidation
  // undiagnosable without correlating timestamps by hand.
  client.on(Events.ShardDisconnect, (event) => {
    const authFailed = event.code === 4004;
    logger.warn(
      `[${botName}] Disconnected (code ${event.code}).` +
      (authFailed
        ? ' Authentication failed — token invalid/rotated; gateway and REST stay dead until a valid token is provisioned and the daemon restarts.'
        : ' discord.js will auto-reconnect.'),
    );
  });

  client.on(Events.ShardReconnecting, () => {
    logger.info(`[${botName}] Reconnecting...`);
  });

  client.on(Events.ShardResume, (_, replayedEvents) => {
    logger.info(`[${botName}] Resumed. Replayed ${replayedEvents} events.`);
  });

  client.on(Events.Error, (error) => {
    logger.error(`[${botName}] Client error: ${error.message}`);
  });

  client.on(Events.Warn, (warning) => {
    logger.warn(`[${botName}] Client warning: ${warning}`);
  });

  return {
    client,
    token,
    channelIdToName,
    allowedChannelIds,
    get botUserId() { return client.user?.id || null; },
    onMessage: (handler) => messageHandlers.push(handler),
    onReaction: (handler) => reactionHandlers.push(handler),

    async start() {
      await client.login(token);
    },

    async stop() {
      client.destroy();
    },

    async sendMessage(channelId, content, options = {}) {
      const channel = await client.channels.fetch(channelId);
      if (!channel) throw new Error(`Channel ${channelId} not found`);
      return sendChunkedDiscordContent(
        content,
        async (chunk, meta) => {
          if (typeof chunk === 'string' && chunk.length > DISCORD_MESSAGE_CONTENT_LIMIT) {
            throw new Error(`Refusing Discord send chunk over ${DISCORD_MESSAGE_CONTENT_LIMIT} characters`);
          }
          const sendOpts = { content: chunk };
          if (meta.is_first && options.embeds) sendOpts.embeds = options.embeds;
          if (meta.is_first && options.reply_to) {
            sendOpts.reply = { messageReference: options.reply_to };
          }
          const msg = await channel.send(sendOpts);
          return {
            message_id: msg.id,
            channel_id: msg.channelId,
            timestamp: msg.createdAt.toISOString(),
          };
        },
      );
    },

    async editMessage(channelId, messageId, content) {
      if (typeof content === 'string' && content.length > DISCORD_MESSAGE_CONTENT_LIMIT) {
        throw new Error(`Refusing Discord edit over ${DISCORD_MESSAGE_CONTENT_LIMIT} characters`);
      }
      const channel = await client.channels.fetch(channelId);
      if (!channel) throw new Error(`Channel ${channelId} not found`);
      const message = await channel.messages.fetch(messageId);
      const edited = await message.edit({ content });
      return {
        message_id: edited.id,
        channel_id: edited.channelId,
        timestamp: (edited.editedAt || edited.createdAt).toISOString(),
      };
    },

    async readMessages(channelId, limit = 25, before = null) {
      const channel = await client.channels.fetch(channelId);
      if (!channel) throw new Error(`Channel ${channelId} not found`);
      const fetchOpts = { limit };
      if (before) fetchOpts.before = before;
      const messages = await channel.messages.fetch(fetchOpts);
      return messages.map(m => ({
        message_id: m.id,
        channel_id: m.channelId,
        author: {
          id: m.author.id,
          username: m.author.username,
          displayName: m.member?.displayName || m.author.displayName || m.author.username,
          bot: m.author.bot,
        },
        content: m.content,
        timestamp: m.createdAt.toISOString(),
        is_reply: !!m.reference,
        reply_to_message_id: m.reference?.messageId || null,
        attachments: m.attachments.map(a => ({ url: a.url, name: a.name })),
        embeds: m.embeds.length,
      })).reverse(); // chronological order
    },

    async addReaction(channelId, messageId, emoji) {
      const channel = await client.channels.fetch(channelId);
      const message = await channel.messages.fetch(messageId);
      await message.react(emoji);
      return { ok: true };
    },

    async getMessageReactions(channelId, messageId) {
      const channel = await client.channels.fetch(channelId);
      const message = await channel.messages.fetch(messageId);
      const botId = client.user?.id;
      for (const reaction of message.reactions.cache.values()) {
        const users = await reaction.users.fetch();
        for (const [userId, user] of users) {
          if (userId !== botId && !user.bot) {
            return { answered: true, type: 'reaction', emoji: reaction.emoji.name, user_id: userId, username: user.username };
          }
        }
      }
      return null;
    },

    async getMessageReplies(channelId, messageId) {
      const channel = await client.channels.fetch(channelId);
      const messages = await channel.messages.fetch({ limit: 50, after: messageId });
      const botId = client.user?.id;
      // messages is a Collection, iterate in insertion order (oldest to newest)
      for (const [, msg] of messages) {
        if (msg.reference?.messageId === messageId && msg.author.id !== botId && !msg.author.bot) {
          return {
            answered: true,
            type: 'reply',
            content: msg.content,
            user_id: msg.author.id,
            username: msg.author.username,
            reply_message_id: msg.id,
          };
        }
      }
      return null;
    },

    async sendDM(content) {
      const user = await client.users.fetch(config.operator_user_id);
      const dm = await user.createDM();
      return sendChunkedDiscordContent(content, async (chunk) => {
        if (chunk.length > DISCORD_MESSAGE_CONTENT_LIMIT) {
          throw new Error(`Refusing Discord DM chunk over ${DISCORD_MESSAGE_CONTENT_LIMIT} characters`);
        }
        const msg = await dm.send({ content: chunk });
        return {
          message_id: msg.id,
          channel_id: msg.channelId,
          timestamp: msg.createdAt.toISOString(),
        };
      });
    },

    getStatus() {
      return {
        // ws.status goes stale after discord.js gives up on an unrecoverable
        // close (4004): a destroyed client kept reporting READY for three days
        // while inbound and REST were dead. destroy() nulls the token — gate
        // connectedness on it.
        connected: client.ws.status === 0 && client.token !== null, // 0 = READY
        token_present: client.token !== null,
        status: client.ws.status,
        ping: client.ws.ping,
        uptime: client.uptime,
        user: client.user?.tag || null,
        guild_id: config.guild_id,
        channels: Object.keys(config.channels).length,
        bot_name: botName,
      };
    },
  };
}

/**
 * Create clients for all configured bots.
 * Returns { mechanicus: client, custodes: client, ... }
 * Bots that fail to load (missing token) are skipped with a warning.
 */
export function createBotClients(config, logger) {
  const clients = {};
  const bots = config.bots || {};
  for (const [name, botConfig] of Object.entries(bots)) {
    try {
      clients[name] = createDiscordClient(config, logger, name, botConfig);
      logger.info(`Bot '${name}' client created`);
    } catch (err) {
      logger.warn(`Bot '${name}' skipped: ${err.message}`);
    }
  }
  // Ensure at least the default mechanicus client exists
  if (Object.keys(clients).length === 0) {
    clients['mechanicus'] = createDiscordClient(config, logger);
  }
  return clients;
}
