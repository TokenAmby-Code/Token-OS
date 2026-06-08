// voice-transcript-router.js — route Discord voice transcripts directly to tmux.
//
// The routing source of truth for persona panes is tmux, not Token API:
//   custodes       -> tmuxctl public target 3:0 (legion:custodes)
//   mechanicus     -> tmuxctl public target 4:0 (mechanicus:fabricator-general)
//   imperial_guard -> daemon-locked live pane converted to a tmuxctl public target
//
// Token API may audit/control higher-level state, but voice delivery must not
// fail because a DB row is stale.

import { execFile, execFileSync } from 'child_process';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const BASE_DIR = join(__dirname, '..');
const CLI_DIR = join(BASE_DIR, 'cli-tools', 'bin');
const TMUXCTL = join(CLI_DIR, 'tmuxctl');
const TMUX_DICTATE = join(CLI_DIR, 'tmux-dictate');
const BOT_TARGETS = {
  // Public tmuxctl aliases in the static DMUX layout. tmuxctl resolves these
  // to the underlying role labels and finally to a physical %pane at send time.
  custodes: '3:0',
  mechanicus: '4:0',
};

const TARGET_MARKER_FALLBACKS = {
  '3:0': 'legion:custodes',
  'legion:custodes': 'legion:custodes',
  '4:0': 'mechanicus:fabricator-general',
  'mechanicus:fabricator-general': 'mechanicus:fabricator-general',
};

export function defaultVoiceTargetForBot(botName) {
  return BOT_TARGETS[normalizeBot(botName)] || null;
}

const TITLE_PREFIX = {
  imperial_guard: 'IG🔒',
  mechanicus: 'MECH🔒',
  custodes: 'CUST🔒',
};

// Cadia voice locks are active-pane locks, so the operator needs an unmistakable
// visual target. This tint is owned only by the voice draft lock lifecycle: set
// when the lock is born, cleared only after the lock is deleted. If a lock goes
// stale, the tint intentionally stays stale with it.
const CADIA_LOCK_STYLE = 'bg=#2b1645';
const VOICE_LOCK_OPTION = '@DISCORD_VOICE_LOCK';

function execFileAsync(file, args, opts = {}) {
  return new Promise((resolve, reject) => {
    execFile(file, args, opts, (err, stdout, stderr) => {
      if (err) {
        err.stdout = stdout;
        err.stderr = stderr;
        reject(err);
        return;
      }
      resolve({ stdout, stderr });
    });
  });
}

function tmuxEnv(extra = {}) {
  return {
    ...process.env,
    PATH: [CLI_DIR, '/opt/homebrew/bin', '/usr/local/bin', process.env.PATH || ''].join(':'),
    ...extra,
  };
}

function normalizeBot(botName) {
  return String(botName || 'unknown').trim().toLowerCase().replaceAll('-', '_');
}

export function normalizeVoiceCommand(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s]+/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

export function parseVoiceCommand(text) {
  let normalized = normalizeVoiceCommand(text);
  if (normalized.startsWith('command ')) normalized = normalized.slice('command '.length).trim();

  const commands = [
    ['scratch that', 'scratch'],
    ['reset target', 'clear'],
    ['clear target', 'clear'],
    ['clear lock', 'clear'],
    ['ship it', 'ship'],
    ['scratch', 'scratch'],
    ['retarget', 'clear'],
    ['unlock', 'clear'],
    ['unmute', 'unmute'],
    ['ship', 'ship'],
    ['mute', 'mute'],
  ];

  for (const [phrase, command] of commands) {
    if (normalized === phrase) return { command, draftText: '' };
    const suffix = ` ${phrase}`;
    if (normalized.endsWith(suffix)) {
      const words = String(text || '').trim().split(/\s+/);
      return { command, draftText: words.slice(0, -phrase.split(' ').length).join(' ').trim() };
    }
  }
  return { command: null, draftText: String(text || '').trim() };
}

function paneExists(pane) {
  if (!pane || !String(pane).startsWith('%')) return false;
  try {
    execFileSync('tmux', ['display-message', '-p', '-t', pane, '#{pane_id}'], {
      encoding: 'utf8',
      timeout: 3000,
      env: tmuxEnv(),
    });
    return true;
  } catch {
    return false;
  }
}

function markerFallbackPane(target) {
  const marker = TARGET_MARKER_FALLBACKS[String(target || '')];
  if (!marker) return null;
  try {
    const out = execFileSync('tmux', ['list-panes', '-a', '-F', '#{pane_id}\t#{@PANE_ID}'], {
      encoding: 'utf8',
      timeout: 3000,
      env: tmuxEnv(),
    });
    for (const line of out.split(/\r?\n/)) {
      const [pane, paneMarker] = line.split('\t', 2);
      if (pane?.startsWith('%') && paneMarker === marker && paneExists(pane)) return pane;
    }
  } catch {
    // caller logs context
  }
  return null;
}

function resolveTargetToPane(target) {
  const raw = String(target || '').trim();
  if (!raw) return null;

  // Physical %pane targets are already fully resolved. Do not ask tmuxctl to
  // reverse-resolve them first: stale tombstones can make a live marked pane
  // such as legion:custodes look absent even while %9 exists.
  if (raw.startsWith('%') && paneExists(raw)) return raw;

  try {
    const out = execFileSync(TMUXCTL, ['resolve-pane', '--format', 'physical', raw], {
      encoding: 'utf8',
      timeout: 5000,
      env: tmuxEnv(),
    }).trim();
    if (out.startsWith('%') && paneExists(out)) return out;
  } catch {
    // Fall through to marker fallback.
  }

  return markerFallbackPane(raw);
}

function publicTargetForPane(pane) {
  try {
    const out = execFileSync(TMUXCTL, ['resolve-pane', '--format', 'id', pane], {
      encoding: 'utf8',
      timeout: 5000,
      env: tmuxEnv(),
    }).trim();
    return out || pane;
  } catch {
    return pane;
  }
}

function displayValue(target, format) {
  const pane = resolveTargetToPane(target);
  if (!pane) return '';
  try {
    return execFileSync('tmux', ['display-message', '-p', '-t', pane, format], {
      encoding: 'utf8',
      timeout: 3000,
      env: tmuxEnv(),
    }).replace(/\n$/, '');
  } catch {
    return '';
  }
}

async function setPaneTitle(target, title) {
  const pane = resolveTargetToPane(target);
  if (!pane) return;
  try {
    await execFileAsync('tmux', ['select-pane', '-t', pane, '-T', title || ''], {
      timeout: 3000,
      env: tmuxEnv({ IMPERIUM_TMUX_AUTOMATION: '1', TMUX_SEND_GATE_ALLOW: 'discord-voice-title' }),
    });
  } catch {
    // title restore is cosmetic
  }
}

async function setPaneStyle(target, style) {
  const pane = resolveTargetToPane(target);
  if (!pane) throw new Error(`target not live: ${target}`);
  await execFileAsync('tmux', ['select-pane', '-t', pane, '-P', style || 'bg=default'], {
    timeout: 3000,
    env: tmuxEnv({ IMPERIUM_TMUX_AUTOMATION: '1', TMUX_SEND_GATE_ALLOW: 'discord-voice-lock-style' }),
  });
}

async function setPaneOption(target, option, value) {
  const pane = resolveTargetToPane(target);
  if (!pane) throw new Error(`target not live: ${target}`);
  await execFileAsync('tmux', ['set-option', '-p', '-t', pane, option, value], {
    timeout: 3000,
    env: tmuxEnv({ IMPERIUM_TMUX_AUTOMATION: '1', TMUX_SEND_GATE_ALLOW: 'discord-voice-lock-option' }),
  });
}

async function applyLockOverlay(state) {
  if (!state?.lockOverlay) return;
  await setPaneOption(state.target, VOICE_LOCK_OPTION, '1');
  await setPaneStyle(state.target, CADIA_LOCK_STYLE);
}

async function restoreLockOverlay(state) {
  if (!state?.lockOverlay) return;
  try {
    await setPaneStyle(state.target, state.paneStyle || 'bg=default');
    await setPaneOption(state.target, VOICE_LOCK_OPTION, '0');
  } catch {
    // Best effort only after the lock has already been cleared.
  }
}

async function typeIntoTarget(target, text, { bypassGuard = false } = {}) {
  const pane = resolveTargetToPane(target);
  if (!pane) throw new Error(`target not live: ${target}`);
  await execFileAsync(TMUX_DICTATE, ['-t', pane, text], {
    timeout: 15_000,
    maxBuffer: 1024 * 1024,
    env: tmuxEnv({
      ...(bypassGuard ? { TMUX_GUARD_SKIP: '1' } : {}),
      TMUX_SEND_GATE_ALLOW: 'discord-voice-direct-input',
      TMUX_SEND_GATE_POLICY: 'pierce',
    }),
  });
}

async function sendKey(target, key) {
  const pane = resolveTargetToPane(target);
  if (!pane) throw new Error(`target not live: ${target}`);
  await execFileAsync('tmux', ['send-keys', '-t', pane, key], {
    timeout: 5000,
    env: tmuxEnv({ IMPERIUM_TMUX_AUTOMATION: '1', TMUX_SEND_GATE_ALLOW: 'discord-voice-command' }),
  });
}

function lockedPaneTarget(result) {
  const pane = result.lockedTmuxPane || result.commitMeta?.lockedTmuxPane || null;
  return paneExists(pane) ? publicTargetForPane(pane) : null;
}

function resolveInitialTarget(bot, result) {
  if (bot === 'imperial_guard') return lockedPaneTarget(result);

  const target = defaultVoiceTargetForBot(bot);
  if (target && resolveTargetToPane(target)) return target;

  // Persona routes prefer stable tmuxctl targets, but if the static alias is
  // missing/stale, use the pane locked at speech start instead of silently
  // dropping the transcript. This preserves Terra/Cadia live behavior while the
  // static layout heals.
  return lockedPaneTarget(result);
}

function summarize(key, state) {
  return {
    bot_name: key.bot,
    author_id: key.userId,
    target: state.target,
    pane: resolveTargetToPane(state.target),
    created_at: state.createdAt,
    utterances: state.utterances || 0,
    pane_alive: !!resolveTargetToPane(state.target),
  };
}

export function createVoiceTranscriptRouter({ logger, voiceManager = null } = {}) {
  const drafts = new Map();

  function keyFor(result) {
    return {
      bot: normalizeBot(result.botName || 'voice'),
      userId: String(result.userId || 'unknown'),
      value: `${normalizeBot(result.botName || 'voice')}:${String(result.userId || 'unknown')}`,
    };
  }

  async function restoreTitle(state) {
    if (state?.target && resolveTargetToPane(state.target)) await setPaneTitle(state.target, state.title || '');
  }

  async function clearDraft(key) {
    const state = drafts.get(key.value);
    if (!state) return null;
    drafts.delete(key.value);
    await restoreTitle(state);
    await restoreLockOverlay(state);
    return summarize(key, state);
  }

  async function route(result) {
    const key = keyFor(result);
    const text = String(result.text || '').trim();
    const parsed = parseVoiceCommand(text);
    let state = drafts.get(key.value);

    if (state && !resolveTargetToPane(state.target)) {
      drafts.delete(key.value);
      logger?.warn?.(`Voice route [${key.bot}/${key.userId}]: locked pane died; cleared draft`);
      state = null;
    }

    if (parsed.command === 'clear') {
      const cleared = await clearDraft(key);
      logger?.info?.(`Voice route [${key.bot}/${key.userId}]: lock clear (${cleared ? 'cleared' : 'none'})`);
      return { routed: true, command: 'clear', cleared: !!cleared };
    }

    if (parsed.command === 'scratch') {
      if (!state) return { routed: false, command: 'scratch', reason: 'no_draft' };
      await sendKey(state.target, 'C-c');
      await clearDraft(key);
      logger?.info?.(`Voice route [${key.bot}/${key.userId}]: scratched ${state.target}`);
      return { routed: true, command: 'scratch', target: state.target, pane: resolveTargetToPane(state.target) };
    }

    if (parsed.command === 'mute') {
      if (parsed.draftText && state) {
        const segment = state.utterances ? ` ${parsed.draftText}` : parsed.draftText;
        await typeIntoTarget(state.target, segment, { bypassGuard: true });
        state.utterances = (state.utterances || 0) + 1;
      }
      const muted = voiceManager?.muteMember
        ? await voiceManager.muteMember(key.userId, key.bot, 15_000).then(r => !!r?.muted).catch(() => false)
        : false;
      return { routed: muted, command: 'mute', muted, temporary: true, duration_ms: 15000 };
    }

    if (parsed.command === 'unmute') {
      const unmuted = voiceManager?.unmuteMember
        ? await voiceManager.unmuteMember(key.userId, key.bot).then(r => !!r?.unmuted).catch(() => false)
        : false;
      return { routed: unmuted, command: 'unmute', unmuted };
    }

    if (parsed.command === 'ship') {
      if (!state) return { routed: false, command: 'ship', reason: 'no_draft' };
      if (parsed.draftText) {
        const segment = state.utterances ? ` ${parsed.draftText}` : parsed.draftText;
        await typeIntoTarget(state.target, segment, { bypassGuard: true });
        state.utterances = (state.utterances || 0) + 1;
      }
      await sendKey(state.target, 'Enter');
      await clearDraft(key);
      logger?.info?.(`Voice route [${key.bot}/${key.userId}]: shipped ${state.target}`);
      return { routed: true, command: 'ship', target: state.target, pane: resolveTargetToPane(state.target) };
    }

    if (!parsed.draftText) return { routed: false, reason: 'empty' };

    if (!state) {
      const target = resolveInitialTarget(key.bot, result);
      const pane = target ? resolveTargetToPane(target) : null;
      if (!target || !pane) {
        logger?.warn?.(`Voice route [${key.bot}/${key.userId}]: no target pane`);
        return { routed: false, reason: 'no_target' };
      }
      const oldTitle = displayValue(target, '#{pane_title}');
      const oldStyle = displayValue(target, '#{pane_style}');
      const prefix = TITLE_PREFIX[key.bot] || `${key.bot.toUpperCase().slice(0, 4)}🔒`;
      if (!oldTitle.startsWith(prefix)) await setPaneTitle(target, `${prefix} ${oldTitle}`.trim());
      state = {
        target,
        title: oldTitle,
        paneStyle: oldStyle,
        lockOverlay: key.bot === 'imperial_guard',
        createdAt: new Date().toISOString(),
        utterances: 0,
      };
      drafts.set(key.value, state);
      try {
        await applyLockOverlay(state);
      } catch (err) {
        drafts.delete(key.value);
        await restoreTitle(state);
        throw err;
      }
      logger?.info?.(`Voice route [${key.bot}/${key.userId}]: locked ${target} (${pane})`);
    }

    const segment = state.utterances ? ` ${parsed.draftText}` : parsed.draftText;
    try {
      // Discord voice is explicit Emperor/user dictation into the locked pane.
      // It pierces the universal recent-typing gate as direct input, while still
      // being audited via TMUX_SEND_GATE_ALLOW.
      await typeIntoTarget(state.target, segment, { bypassGuard: true });
    } catch (err) {
      if ((state.utterances || 0) === 0) {
        drafts.delete(key.value);
        await restoreTitle(state);
        await restoreLockOverlay(state);
      }
      throw err;
    }
    state.utterances = (state.utterances || 0) + 1;
    return { routed: true, drafting: true, target: state.target, pane: resolveTargetToPane(state.target) };
  }

  return {
    route,
    listDrafts() {
      return [...drafts.entries()].map(([value, state]) => {
        const [bot, userId] = value.split(':', 2);
        return summarize({ bot, userId, value }, state);
      });
    },
    async clear(filter = {}) {
      const cleared = [];
      for (const value of [...drafts.keys()]) {
        const [bot, userId] = value.split(':', 2);
        if (filter.bot && filter.bot !== bot) continue;
        if (filter.userId && filter.userId !== userId) continue;
        const item = await clearDraft({ bot, userId, value });
        if (item) cleared.push(item);
      }
      return cleared;
    },
  };
}
