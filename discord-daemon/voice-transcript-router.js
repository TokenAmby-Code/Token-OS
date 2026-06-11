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
  fabricator_general: '4:0',
  fg: '4:0',
};

export function defaultVoiceTargetForBot(botName) {
  return BOT_TARGETS[normalizeBot(botName)] || null;
}

const TITLE_PREFIX = {
  imperial_guard: 'IG🔒',
  mechanicus: 'MECH🔒',
  custodes: 'CUST🔒',
};

// Cadia voice locks are active-pane locks. The operator-visible signal is the
// tmux status bar reading these pane options (tmux-base.conf pane-border-format);
// the lock deliberately owns no pane styling. @DISCORD_VOICE_LOCK is the source
// of truth for ownership; @DISCORD_VOICE_PROCESSING is 1 only while transcript
// text is actively being applied to the pane.
const VOICE_LOCK_OPTION = '@DISCORD_VOICE_LOCK';
const VOICE_PROCESSING_OPTION = '@DISCORD_VOICE_PROCESSING';
const TMUX_RESOLVE_TIMEOUT_MS = Number(process.env.DISCORD_VOICE_TMUX_RESOLVE_TIMEOUT_MS || 1500);
const TMUX_WRITE_TIMEOUT_MS = Number(process.env.DISCORD_VOICE_TMUX_WRITE_TIMEOUT_MS || 8000);
const TMUX_COMMAND_TIMEOUT_MS = Number(process.env.DISCORD_VOICE_TMUX_COMMAND_TIMEOUT_MS || 3000);

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

// Read-only tmux state lookups (pane existence, the static-target list-panes,
// title reads) must resolve to the LOCAL tmux binary, never the NAS-hosted
// cli-tools/bin/tmux guard wrapper. That wrapper lives on the SMB mount; when
// the mount stalls, spawning it times out (spawnSync tmux ETIMEDOUT) and a live
// voice transcript is silently dropped as "no target pane" — observed dropping
// both a Custodes static-target route and a Cadia locked-pane route. The guard
// wrapper exists for interactive typing/focus safety, which read lookups never
// need, so prefer the local binary here. Writes keep tmuxEnv()'s wrapper path
// (they rely on its TMUX_SEND_GATE_ALLOW handling).
function tmuxReadEnv(extra = {}) {
  return {
    ...process.env,
    PATH: ['/opt/homebrew/bin', '/usr/local/bin', CLI_DIR, process.env.PATH || ''].join(':'),
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

function paneExists(pane, execSync = execFileSync) {
  if (!pane || !String(pane).startsWith('%')) return false;
  try {
    const out = execSync('tmux', ['display-message', '-p', '-t', pane, '#{pane_id}'], {
      encoding: 'utf8',
      timeout: TMUX_RESOLVE_TIMEOUT_MS,
      env: tmuxReadEnv(),
    }).trim();
    return out === pane || out.startsWith('%');
  } catch {
    return false;
  }
}

function staticTargetSpec(target) {
  const raw = String(target || '').trim();
  if (raw === '3:0' || raw === 'legion:custodes') {
    return { target: raw, windowIndex: '3', marker: 'legion:custodes' };
  }
  if (raw === '4:0' || raw === 'mechanicus:fabricator-general') {
    return { target: raw, windowIndex: '4', marker: 'mechanicus:fabricator-general' };
  }
  return null;
}

export function resolveStaticVoiceTargetToPane(target, {
  execSync = execFileSync,
  paneExistsFn = pane => paneExists(pane, execSync),
  logger = null,
} = {}) {
  const spec = staticTargetSpec(target);
  if (!spec) return null;

  let markerResult = null;
  let staticWindowResult = null;
  let tmuxError = null;

  try {
    const out = execSync('tmux', [
      'list-panes',
      '-a',
      '-F',
      '#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_id}\t#{@PANE_ID}',
    ], {
      encoding: 'utf8',
      timeout: TMUX_RESOLVE_TIMEOUT_MS,
      env: tmuxReadEnv(),
    });
    const marked = [];
    const fallback = [];
    for (const line of out.split(/\r?\n/)) {
      if (!line) continue;
      const [sessionName, win, paneIndex, pane, paneMarker] = line.split('\t', 5);
      if (sessionName !== 'main' || win !== spec.windowIndex || !pane?.startsWith('%')) continue;
      if (!paneExistsFn(pane)) continue;
      const index = Number.parseInt(paneIndex || '9999', 10);
      const candidate = { pane, index: Number.isFinite(index) ? index : 9999 };
      if (paneMarker === spec.marker) marked.push(candidate);
      fallback.push(candidate);
    }
    marked.sort((a, b) => a.index - b.index);
    fallback.sort((a, b) => a.index - b.index);
    markerResult = marked[0]?.pane || null;
    staticWindowResult = fallback[0]?.pane || null;
    if (markerResult) return markerResult;
    if (staticWindowResult) return staticWindowResult;
  } catch (err) {
    tmuxError = err?.message || String(err);
  }

  logger?.warn?.(
    `Voice static target resolve failed: target=${spec.target} ` +
    `marker=${markerResult || 'none'} static_window=${staticWindowResult || 'none'} ` +
    `tmux_error=${tmuxError || 'none'} TMUX=${process.env.TMUX ? 'set' : 'unset'}`
  );
  return null;
}

export function resolveTargetToPane(target, { logger = null } = {}) {
  const raw = String(target || '').trim();
  if (!raw) return null;

  // Physical %pane targets are already fully resolved.
  if (raw.startsWith('%') && paneExists(raw)) return raw;

  // Daemon-owned static voice persona resolver. Custodes and FG must not pass
  // through tmuxctl/tombstone state and must never fall back to Cadia's active pane.
  const staticPane = resolveStaticVoiceTargetToPane(raw, { logger });
  if (staticPane) return staticPane;

  try {
    const out = execFileSync(TMUXCTL, ['resolve-pane', '--format', 'physical', raw], {
      encoding: 'utf8',
      timeout: TMUX_RESOLVE_TIMEOUT_MS,
      env: tmuxEnv(),
    }).trim();
    if (out.startsWith('%') && paneExists(out)) return out;
  } catch {
    // Non-static dynamic targets may still be absent.
  }

  return null;
}

function publicTargetForPane(pane) {
  try {
    const out = execFileSync(TMUXCTL, ['resolve-pane', '--format', 'id', pane], {
      encoding: 'utf8',
      timeout: TMUX_RESOLVE_TIMEOUT_MS,
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
      timeout: TMUX_RESOLVE_TIMEOUT_MS,
      env: tmuxReadEnv(),
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
      timeout: TMUX_COMMAND_TIMEOUT_MS,
      env: tmuxEnv({ IMPERIUM_TMUX_AUTOMATION: '1', TMUX_SEND_GATE_ALLOW: 'discord-voice-title' }),
    });
  } catch {
    // title restore is cosmetic
  }
}

async function setPaneOption(target, option, value) {
  const pane = resolveTargetToPane(target);
  if (!pane) throw new Error(`target not live: ${target}`);
  await execFileAsync('tmux', ['set-option', '-p', '-t', pane, option, value], {
    timeout: TMUX_COMMAND_TIMEOUT_MS,
    env: tmuxEnv({ IMPERIUM_TMUX_AUTOMATION: '1', TMUX_SEND_GATE_ALLOW: 'discord-voice-lock-option' }),
  });
}

async function typeIntoTarget(target, text, { bypassGuard = false } = {}) {
  const pane = resolveTargetToPane(target);
  if (!pane) throw new Error(`target not live: ${target}`);
  await execFileAsync(TMUX_DICTATE, ['-t', pane, text], {
    timeout: TMUX_WRITE_TIMEOUT_MS,
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
    timeout: TMUX_COMMAND_TIMEOUT_MS,
    env: tmuxEnv({ IMPERIUM_TMUX_AUTOMATION: '1', TMUX_SEND_GATE_ALLOW: 'discord-voice-command' }),
  });
}

function lockedPaneTarget(result) {
  const pane = result.lockedTmuxPane || result.commitMeta?.lockedTmuxPane || null;
  return paneExists(pane) ? publicTargetForPane(pane) : null;
}

export function selectInitialVoiceTarget(botName, lockedTarget = null) {
  const bot = normalizeBot(botName);
  if (bot === 'imperial_guard') return lockedTarget;

  const target = defaultVoiceTargetForBot(bot);
  if (target) return target;

  return null;
}

export function createVoiceTranscriptRouter({
  logger,
  voiceManager = null,
  resolveTargetToPane: resolvePane = resolveTargetToPane,
  displayValue: readDisplayValue = displayValue,
  setPaneTitle: writePaneTitle = setPaneTitle,
  setPaneOption: writePaneOption = setPaneOption,
  typeIntoTarget: writeText = typeIntoTarget,
  sendKey: writeKey = sendKey,
  lockedPaneTarget: resolveLockedPaneTarget = lockedPaneTarget,
} = {}) {
  const drafts = new Map();
  const resolvePaneWithDiagnostics = resolvePane === resolveTargetToPane
    ? target => resolveTargetToPane(target, { logger })
    : resolvePane;

  function keyFor(result) {
    return {
      bot: normalizeBot(result.botName || 'voice'),
      userId: String(result.userId || 'unknown'),
      value: `${normalizeBot(result.botName || 'voice')}:${String(result.userId || 'unknown')}`,
    };
  }

  async function restoreTitle(state) {
    if (state?.target && resolvePaneWithDiagnostics(state.target)) await writePaneTitle(state.target, state.title || '');
  }

  async function setStateProcessing(state, processing) {
    if (!state?.lockOverlay) return;
    await writePaneOption(state.target, VOICE_PROCESSING_OPTION, processing ? '1' : '0');
  }

  async function applyStateLockOverlay(state) {
    if (!state?.lockOverlay) return;
    await writePaneOption(state.target, VOICE_LOCK_OPTION, '1');
    // Cosmetic init of the processing flag — best-effort, must not fail acquire.
    try {
      await writePaneOption(state.target, VOICE_PROCESSING_OPTION, '0');
    } catch {}
  }

  async function restoreStateLockOverlay(state) {
    if (!state?.lockOverlay) return;
    try {
      await writePaneOption(state.target, VOICE_PROCESSING_OPTION, '0');
    } catch {
      // Best effort.
    }
    try {
      await writePaneOption(state.target, VOICE_LOCK_OPTION, '0');
    } catch {
      // Best effort. Ownership clearing must not depend on the processing write.
    }
  }

  function summarizeDraft(key, state) {
    const pane = resolvePaneWithDiagnostics(state.target);
    return {
      bot_name: key.bot,
      author_id: key.userId,
      target: state.target,
      pane,
      created_at: state.createdAt,
      utterances: state.utterances || 0,
      pane_alive: !!pane,
    };
  }

  function resolveInitialTargetForResult(bot, result) {
    const normalizedBot = normalizeBot(bot);
    const lockedTarget = normalizedBot === 'imperial_guard' ? resolveLockedPaneTarget(result) : null;
    return selectInitialVoiceTarget(normalizedBot, lockedTarget);
  }

  async function clearDraft(key) {
    const state = drafts.get(key.value);
    if (!state) return null;
    drafts.delete(key.value);
    await restoreTitle(state);
    await restoreStateLockOverlay(state);
    return summarizeDraft(key, state);
  }

  async function clearDrafts(filter = {}) {
    const cleared = [];
    const filterBot = filter.bot ? normalizeBot(filter.bot) : null;
    const filterUserId = filter.userId ? String(filter.userId) : null;
    for (const value of [...drafts.keys()]) {
      const [bot, userId] = value.split(':', 2);
      if (filterBot && filterBot !== bot) continue;
      if (filterUserId && filterUserId !== userId) continue;
      const item = await clearDraft({ bot, userId, value });
      if (item) cleared.push(item);
    }
    return cleared;
  }

  async function appendDraftText(state, draftText) {
    const segment = state.utterances ? ` ${draftText}` : draftText;
    // Discord voice is explicit Emperor/user dictation into the locked pane.
    // It pierces the universal recent-typing gate as direct input, while still
    // being audited via TMUX_SEND_GATE_ALLOW.
    // The processing flag is operator-visible status only — a flaky option
    // write must never block transcript delivery, so both edges are best-effort.
    try { await setStateProcessing(state, true); } catch {}
    try {
      await writeText(state.target, segment, { bypassGuard: true });
    } finally {
      // The text already landed (or the error is propagating); a failed
      // processing-clear must not fail the route or strand the draft.
      try { await setStateProcessing(state, false); } catch {}
    }
    state.utterances = (state.utterances || 0) + 1;
  }

  async function route(result) {
    const key = keyFor(result);
    const text = String(result.text || '').trim();
    const parsed = parseVoiceCommand(text);
    let state = drafts.get(key.value);

    const botStatus = voiceManager?.getStatus?.(key.bot);
    if (botStatus) {
      const resultEpoch = result.routeEpoch ?? result.commitMeta?.routeEpoch;
      const resultChannelId = result.channelId ?? result.commitMeta?.channelId;
      const currentEpoch = botStatus.routeEpoch;
      const currentChannelId = botStatus.channelId ?? null;
      const epochMismatch = resultEpoch !== undefined && currentEpoch !== undefined && Number(resultEpoch) !== Number(currentEpoch);
      const channelMismatch = resultChannelId !== undefined && String(resultChannelId || '') !== String(currentChannelId || '');
      if (epochMismatch || channelMismatch) {
        const cleared = await clearDrafts({ bot: key.bot, userId: key.userId });
        logger?.warn?.(
          `Voice route [${key.bot}/${key.userId}]: ignored stale transcript ` +
          `(epoch=${resultEpoch ?? 'none'} current_epoch=${currentEpoch ?? 'none'} ` +
          `channel=${resultChannelId || 'none'} current_channel=${currentChannelId || 'none'} cleared=${cleared.length})`
        );
        return { routed: false, ignored: true, reason: 'stale_transcript', cleared: cleared.length };
      }

      if (!botStatus.connected || !botStatus.listening) {
        const cleared = await clearDrafts({ bot: key.bot, userId: key.userId });
        logger?.warn?.(
          `Voice route [${key.bot}/${key.userId}]: ignored transcript after bot left ` +
          `(connected=${!!botStatus.connected}, listening=${!!botStatus.listening}, cleared=${cleared.length})`
        );
        return { routed: false, ignored: true, reason: 'bot_not_connected', cleared: cleared.length };
      }
    }

    if (state && !resolvePaneWithDiagnostics(state.target)) {
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
      await writeKey(state.target, 'C-c');
      await clearDraft(key);
      logger?.info?.(`Voice route [${key.bot}/${key.userId}]: scratched ${state.target}`);
      return { routed: true, command: 'scratch', target: state.target, pane: resolvePaneWithDiagnostics(state.target) };
    }

    if (parsed.command === 'mute') {
      if (parsed.draftText && state) {
        await appendDraftText(state, parsed.draftText);
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
        await appendDraftText(state, parsed.draftText);
      }
      await writeKey(state.target, 'Enter');
      await clearDraft(key);
      logger?.info?.(`Voice route [${key.bot}/${key.userId}]: shipped ${state.target}`);
      return { routed: true, command: 'ship', target: state.target, pane: resolvePaneWithDiagnostics(state.target) };
    }

    if (!parsed.draftText) return { routed: false, reason: 'empty' };

    if (!state) {
      const target = resolveInitialTargetForResult(key.bot, result);
      const pane = target ? resolvePaneWithDiagnostics(target) : null;
      if (!target || !pane) {
        logger?.warn?.(`Voice route [${key.bot}/${key.userId}]: no target pane for ${target || 'none'}`);
        return { routed: false, reason: 'no_target' };
      }
      const oldTitle = readDisplayValue(target, '#{pane_title}');
      const prefix = TITLE_PREFIX[key.bot] || `${key.bot.toUpperCase().slice(0, 4)}🔒`;
      if (!oldTitle.startsWith(prefix)) await writePaneTitle(target, `${prefix} ${oldTitle}`.trim());
      state = {
        target,
        title: oldTitle,
        lockOverlay: key.bot === 'imperial_guard',
        createdAt: new Date().toISOString(),
        utterances: 0,
      };
      drafts.set(key.value, state);
      try {
        await applyStateLockOverlay(state);
      } catch (err) {
        drafts.delete(key.value);
        await restoreTitle(state);
        await restoreStateLockOverlay(state);
        throw err;
      }
      logger?.info?.(`Voice route [${key.bot}/${key.userId}]: locked ${target} (${pane})`);
    }

    try {
      await appendDraftText(state, parsed.draftText);
    } catch (err) {
      if ((state.utterances || 0) === 0) {
        drafts.delete(key.value);
        await restoreTitle(state);
        await restoreStateLockOverlay(state);
      }
      throw err;
    }
    return { routed: true, drafting: true, target: state.target, pane: resolvePaneWithDiagnostics(state.target) };
  }

  return {
    route,
    listDrafts() {
      return [...drafts.entries()].map(([value, state]) => {
        const [bot, userId] = value.split(':', 2);
        return summarizeDraft({ bot, userId, value }, state);
      });
    },
    async clear(filter = {}) {
      return clearDrafts(filter);
    },
  };
}
