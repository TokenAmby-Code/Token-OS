// discord-client.js — discord.js v14 connection management
// Subscribes to ALL messages in configured channels (not just pings)

import { Client, GatewayIntentBits, Partials, Events } from 'discord.js';
import { readFileSync } from 'fs';
import { execSync } from 'child_process';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = join(__dirname, '..', 'config.json');

export function loadConfig() {
  return JSON.parse(readFileSync(CONFIG_PATH, 'utf-8'));
}

function getToken(config, botConfig = null) {
  // If a specific bot config is provided, use its keychain_service
  const keychainService = botConfig?.keychain_service
    || config.token_keychain_service
    || config.bots?.mechanicus?.keychain_service;

  if (config.token_source === 'keychain' && keychainService) {
    try {
      const token = execSync(
        `security find-generic-password -s "${keychainService}" -w`,
        { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] }
      ).trim();
      if (token) return token;
    } catch {
      // Fall through to fallback
    }
  }

  // Fallback: read from openclaw.json
  if (config.token_fallback_file && config.token_fallback_path) {
    try {
      const filePath = config.token_fallback_file.replace('~', process.env.HOME);
      const data = JSON.parse(readFileSync(filePath, 'utf-8'));
      const keys = config.token_fallback_path.split('.');
      let val = data;
      for (const k of keys) val = val[k];
      return val;
    } catch {
      // Fall through
    }
  }

  throw new Error(`No Discord bot token found for bot${botConfig ? ` (${botConfig.keychain_service})` : ''}`);
}

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

  const client = new Client(isListener ? {
    // Full intents: read ALL message content, DMs, reactions
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMessages,
      GatewayIntentBits.MessageContent,
      GatewayIntentBits.DirectMessages,
      GatewayIntentBits.GuildMessageReactions,
    ],
    partials: [
      Partials.Channel,
      Partials.Message,
      Partials.Reaction,
    ],
  } : {
    // Minimal intents: send-only bots only need Guilds to fetch channels
    intents: [GatewayIntentBits.Guilds],
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

    if (!isAllowedChannel && !isOperatorDM) return;

    const channelName = isDM ? 'dm' : (channelIdToName[message.channelId] || message.channelId);

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
    };

    logger.debug(`[${channelName}] ${msgData.author.username}: ${message.content.slice(0, 80)}`);

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
  // but we log the events
  client.on(Events.ShardDisconnect, (event) => {
    logger.warn(`Disconnected (code ${event.code}). discord.js will auto-reconnect.`);
  });

  client.on(Events.ShardReconnecting, () => {
    logger.info('Reconnecting...');
  });

  client.on(Events.ShardResume, (_, replayedEvents) => {
    logger.info(`Resumed. Replayed ${replayedEvents} events.`);
  });

  client.on(Events.Error, (error) => {
    logger.error(`Client error: ${error.message}`);
  });

  client.on(Events.Warn, (warning) => {
    logger.warn(`Client warning: ${warning}`);
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
      const sendOpts = { content };
      if (options.embeds) sendOpts.embeds = options.embeds;
      if (options.reply_to) {
        sendOpts.reply = { messageReference: options.reply_to };
      }
      const msg = await channel.send(sendOpts);
      return {
        message_id: msg.id,
        channel_id: msg.channelId,
        timestamp: msg.createdAt.toISOString(),
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
      const msg = await dm.send({ content });
      return {
        message_id: msg.id,
        channel_id: msg.channelId,
        timestamp: msg.createdAt.toISOString(),
      };
    },

    getStatus() {
      return {
        connected: client.ws.status === 0, // 0 = READY
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
