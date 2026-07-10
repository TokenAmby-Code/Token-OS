// ─────────────────────────────────────────────────────────────────────────
// Ops Cockpit — data layer: cockpit-local types + pure live-contract adapters.
//
// This module owns the shapes the cockpit components render (CockpitTimerPoint,
// DialModel, TtsItem, …) and the PURE functions that project the live Token-API
// read-models (OpsState / TimerHistory, via src/contracts.ts) onto them. No
// fetching lives here — src/api.ts owns the polling hooks; the root component
// runs these adapters over each feed and provides the result via context.
//
// Honesty rule: an adapter maps only what the live contract actually carries.
// Subsystems the contract doesn't cover this phase (enforce, gt, mac, wsl,
// mesh) render an explicit '—' placeholder dial — never a frozen fake value.
// ─────────────────────────────────────────────────────────────────────────

import type { OpsSourceHealth, OpsState, PipelineDoc, TimerHistory, TimerMode } from './contracts';
import type { CompassStar } from './compass';

export type CockpitMode = 'working' | 'multitasking' | 'distracted' | 'break' | 'idle';

export type CockpitTimerPoint = {
  t: string; // "HH:MM" local
  mode: CockpitMode;
  breakBalanceMinutes: number; // signed: >0 credit, <0 debt
};

export type CockpitModeSegment = {
  start: string; // "HH:MM"
  end: string; // "HH:MM"
  mode: CockpitMode;
};

// ── time helpers (pure) ────────────────────────────────────────────────────
export const toMin = (hhmm: string): number => {
  const [h, m] = hhmm.split(':').map(Number);
  return h * 60 + m;
};
export const toClock = (min: number): string => {
  const h = Math.floor(min / 60);
  const m = Math.round(min % 60);
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
};

/** ISO timestamp → local "HH:MM" (the cockpit's clock-string coordinate). */
export const isoToClock = (iso: string): string => {
  const d = new Date(iso);
  return toClock(d.getHours() * 60 + d.getMinutes());
};

/** Local "HH:MM" of right now — the live now-point's time coordinate. */
export const nowClock = (): string => {
  const d = new Date();
  return toClock(d.getHours() * 60 + d.getMinutes());
};

// ── state dials (the floating radial cluster) ──────────────────────────────
export type DialTone = 'good' | 'warn' | 'bad' | 'neutral' | 'idle';

// A dial's click contract lives in its TYPE. Omit `action` and the dial does
// the default thing — opens the dials drawer. Provide an override and the
// generic <Dial> component runs that dial's own on-click feature instead. New
// override kinds are added here (and to <Dial>'s switch) as features land.
export type DialAction =
  | { kind: 'toggle-timer' } // timer dial → pause/resume the running timer
  | { kind: 'dismiss-phone' } // phone dial → force-clear stuck phone attention
  | { kind: 'ack-enforce' }; // enforce dial → acknowledge the pending enforcement

export type DialModel = {
  id: string;
  label: string;
  glyph: string;
  value: string;
  tone: DialTone;
  noteworthy: boolean;
  subtitle: string; // "what is this dial?" subheader — hover tip + drawer line
  tag?: string; // optional mono id chip shown before the label in the hover tip
  //               (the TTS stack uses it for the sender's short instance id)
  action?: DialAction; // omit → default click opens the dials drawer
};

// ── TTS queue (the left-side stack) ─────────────────────────────────────────
// Modelled as a QUEUE, not a flat status list. `posInQueue` is the order key
// (0 = head = currently speaking); `status` drives the dial's tone + glyph in
// the render. Live data only ever yields 'speaking'/'queued' — nothing lingers
// as 'done'; the stack simply renders shorter as the queue drains.
export type TtsItemStatus = 'speaking' | 'queued' | 'done';

export type TtsQueueState = 'current' | 'hot' | 'pause';

export type TtsItem = {
  id: string;
  itemKey: string | undefined;
  queueState: TtsQueueState | undefined;
  promotable: boolean;
  text: string; // the utterance — surfaced in the hover tip + drawer
  route: string; // sender / delivery route (e.g. "hot · Custodes")
  senderInstanceId: string; // sender's FULL instance id — joins the utterance to its
  //                           live worker chip (the edge-A flight source)
  senderTmuxId: string; // sender's short instance-id form (the contract carries no pane id)
  senderName: string; // sender's instance-name (the live session's descriptive name)
  persona: string; // sender's persona key → its icon (see src/personaIcons.tsx).
  //                   Lower-kebab, matching the registry keys (vault/DB slugs).
  commanderType?: string | null; // backend sender commander_type; chapter is duplicate-glow exempt.
  playbackTarget?: string | null;
  status: TtsItemStatus;
  posInQueue: number; // 0-based order key; head (0) is the one speaking
  durationMs?: number; // speak length hint; the live contract doesn't carry one,
  //                      so live items omit it.
};

// The queue-languishing threshold — a GENERIC concept that lives in the data
// layer alongside the other enforcement events (once more than this many
// utterances back up, the queue is "languishing"). The left stack borrows it as
// a convenient VISUAL marker: the TTS dial size/gap are tuned so about this many
// dials fit above the connecting arc's left-edge contact, so dials that spill
// BELOW the arc read as the languishing overflow. The coupling is one-way and
// cosmetic — the arc itself stays FROZEN and is NOT derived from this value (not
// a hot code path); we just tune the packing to land near it.
export const ttsLanguishThreshold = 8;

// ─────────────────────────────────────────────────────────────────────────
// Live-contract adapters — pure OpsState / TimerHistory → cockpit-model maps.
// ─────────────────────────────────────────────────────────────────────────

/**
 * Live TimerMode → the cockpit's five-mode palette. The graph and dials render
 * exactly five modes; the live engine has a longer tail:
 *   sleeping / quiet → 'idle' (the operator is off the instruments),
 *   morning_session  → 'working' (a first-class focused block),
 *   anything unknown → 'idle' (never guess a hotter mode than the data proves).
 */
export function mapMode(mode: TimerMode): CockpitMode {
  switch (mode) {
    case 'working':
    case 'multitasking':
    case 'distracted':
    case 'break':
    case 'idle':
      return mode;
    case 'sleeping':
    case 'quiet':
      return 'idle';
    case 'morning_session':
      return 'working';
    default:
      return 'idle';
  }
}

/** Signed break-balance ms → minutes, rounded to 0.1 (the graph's y unit). */
export const balanceMinutes = (ms: number): number => Math.round((ms / 60000) * 10) / 10;

/** TimerHistory points → the graph's sampled balance line. */
export function toTimerPoints(h: TimerHistory): CockpitTimerPoint[] {
  return h.points.map((p) => ({
    t: isoToClock(p.t),
    mode: mapMode(p.mode),
    breakBalanceMinutes: balanceMinutes(p.break_balance_ms),
  }));
}

/**
 * TimerHistory segments → the graph's mode bands. The live read-model returns
 * near per-sample segments (~one per bucket), not per-mode spans — so
 * consecutive segments whose MAPPED mode is equal are coalesced into one run
 * here, and the TimerField renders a handful of band rects instead of hundreds.
 */
export function toModeSegments(h: TimerHistory): CockpitModeSegment[] {
  const out: CockpitModeSegment[] = [];
  for (const s of h.segments) {
    const mode = mapMode(s.mode);
    const last = out[out.length - 1];
    if (last && last.mode === mode) {
      last.end = isoToClock(s.end); // extend the running same-mode span
    } else {
      out.push({ start: isoToClock(s.start), end: isoToClock(s.end), mode });
    }
  }
  return out;
}

// source-health → dial readout. 'nominal'/'degraded'/'down' with matching tone;
// an 'unknown' health reads as an explicit unknown, not a fake nominal.
function healthDial(h: OpsSourceHealth): { value: string; tone: DialTone } {
  switch (h.status) {
    case 'ok':
      return { value: 'nominal', tone: 'good' };
    case 'warn':
      return { value: 'degraded', tone: 'warn' };
    case 'bad':
      return { value: 'down', tone: 'bad' };
    default:
      return { value: 'unknown', tone: 'neutral' };
  }
}


export function ttsDial(s: OpsState): DialModel {
  const h = healthDial(s.sources.tts);
  if (h.tone === 'bad' || h.tone === 'neutral') {
    return { id: 'tts', label: 'TTS', glyph: '♪', value: h.value, tone: h.tone, noteworthy: true, subtitle: `Text-to-speech queue — ${s.sources.tts.message ?? h.value}.` };
  }
  const hot = s.tts.hot_queue_length ?? s.tts.hot_queue?.length ?? 0;
  const pause = s.tts.pause_queue_length ?? s.tts.pause_queue?.length ?? 0;
  const speaking = Boolean(s.tts.current);
  const value = speaking ? 'speaking' : hot ? `hot ${hot}` : pause ? `pause ${pause}` : 'idle';
  const tone: DialTone = speaking || hot ? 'warn' : pause ? 'neutral' : 'good';
  return { id: 'tts', label: 'TTS', glyph: '♪', value, tone, noteworthy: speaking || hot > 0 || pause > 0 || h.tone !== 'good', subtitle: `Text-to-speech queue — hot ${hot}, pause ${pause}, backend ${s.tts.backend ?? 'unknown'}, satellite ${String(s.tts.satellite_available)}.` };
}

export function enforcementDial(s: OpsState): DialModel {
  const h = healthDial(s.sources.enforcement);
  const pending = s.enforcement.pending_count ?? 0;
  const pavlok = s.enforcement.pavlok ?? {};
  const pavlokEnabled = typeof pavlok.enabled === 'boolean' ? `Pavlok ${pavlok.enabled ? 'on' : 'off'}` : 'Pavlok unknown';
  const sourceBad = h.tone === 'bad' || h.tone === 'neutral';
  return {
    id: 'enforce', label: 'Enforce', glyph: '!',
    value: sourceBad ? h.value : pending ? `pending ${pending}` : 'clear',
    tone: sourceBad ? h.tone : pending ? 'bad' : 'good',
    noteworthy: sourceBad || pending > 0,
    subtitle: `Enforcement queue — ${pavlokEnabled}${s.enforcement.error ? `; ${s.enforcement.error}` : ''}.`,
    ...(pending > 0 ? { action: { kind: 'ack-enforce' } as DialAction } : {}),
  };
}

export function goldenThroneDial(s: OpsState): DialModel {
  const active = s.instances.active.map((i) => i.gt).filter(Boolean);
  const due = active.filter((gt) => gt.next_fire && Date.parse(gt.next_fire) <= Date.now()).length;
  const armed = active.filter((gt) => gt.next_fire).length;
  const resume = active.reduce((n, gt) => n + (gt.resume_count ?? 0), 0);
  const victory = active.filter((gt) => gt.victory_at).length;
  const value = due ? `due ${due}` : resume ? `resume ${resume}` : armed ? `armed ${armed}` : victory ? `victory ${victory}` : 'clear';
  const tone: DialTone = due ? 'bad' : resume || armed ? 'warn' : victory ? 'good' : 'idle';
  return { id: 'gt', label: 'Gold. Throne', glyph: '♛', value, tone, noteworthy: due > 0 || resume > 0 || armed > 0, subtitle: `Golden Throne rubrics — ${armed} armed, ${resume} resume signal(s), ${victory} victory ack(s).` };
}

function sourceDial(s: OpsState): DialModel {
  const degraded = Object.values(s.sources ?? {}).filter((src) => ['warn', 'bad', 'unknown'].includes(src?.status ?? 'unknown')).length;
  return {
    id: 'sources', label: 'Sources', glyph: '◇', value: degraded ? `${degraded} degraded` : 'nominal',
    tone: degraded ? 'warn' : 'good', noteworthy: degraded > 0,
    subtitle: `Aggregate source health — ${degraded} degraded source(s).`,
  };
}

function fleetDial(s: OpsState): DialModel {
  const c = s.instances.counts;
  const engines = Object.entries(c.by_engine ?? {}).map(([k, v]) => `${k}:${v}`).join(' ') || 'engines unknown';
  return {
    id: 'fleet', label: 'Fleet', glyph: '◆', value: `${c.active} active`,
    tone: c.stale ? 'warn' : c.active ? 'good' : 'idle', noteworthy: c.stale > 0,
    subtitle: `Instance registry — ${c.active} active, ${c.stale} stale; ${engines}.`,
  };
}

function workDial(s: OpsState): DialModel {
  const w = s.work_state;
  const typing = w.typing_active ? 'typing' : 'not typing';
  const hold = w.productivity_hold ? `; hold ${w.productivity_hold}` : '';
  return {
    id: 'work', label: 'Work', glyph: '⌁', value: w.productivity_active ? 'active' : 'idle',
    tone: w.productivity_active ? 'good' : 'neutral', noteworthy: Boolean(w.productivity_hold || w.typing_active),
    subtitle: `Productivity state — ${w.reason}; ${typing}${hold}.`,
  };
}

function tmuxDial(s: OpsState): DialModel {
  const occ = s.tmux.occupancy;
  const reachable = s.tmux.reachable === true;
  const drift = occ?.drift ?? 0;
  const dead = occ?.dead ?? 0;
  const value = !reachable ? 'unreachable' : occ ? `${occ.occupied}/${occ.total} used` : 'unknown';
  const tone: DialTone = !reachable ? 'bad' : occ?.status === 'bad' ? 'bad' : drift || dead || occ?.status === 'warn' ? 'warn' : 'good';
  return {
    id: 'tmux', label: 'tmux', glyph: '▦', value, tone, noteworthy: tone !== 'good',
    subtitle: `tmuxctld occupancy — free ${occ?.free ?? 0}, dead ${dead}, drift ${drift}${occ?.errors?.length ? `; ${occ.errors.join('; ')}` : ''}.`,
  };
}

const COMPASS_DIRECTIONS = new Set(['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']);
export const OCCUPANCY_COMPASS_FALLBACK_STARS: CompassStar[] = [{ dir: 'N', color: 'red' }];

function paneCompassStar(cell: { pane_positional_id?: string | null; state?: string | null }): CompassStar | null {
  // Stable pane roles arrive as palace:N / somnium:NE. Some tmuxctld views can
  // expose the equivalent numeric window positions, where 1 is palace and 2 is
  // somnium. The compass reducer already handles coalescing and red+blue=purple;
  // this adapter only translates occupied slots into authored stars.
  const raw = String(cell.pane_positional_id ?? '');
  const [page, pos] = raw.split(':');
  if (!page || !pos || !COMPASS_DIRECTIONS.has(pos)) return null;
  const color = page === 'palace' || page === '1' ? 'red' : page === 'somnium' || page === '2' ? 'blue' : null;
  if (!color) return null;
  const state = cell.state ?? 'unknown';
  if (!['occupied', 'protected', 'drift'].includes(state)) return null;
  return { dir: pos as CompassStar['dir'], color };
}

export function occupancyCompassStars(s: OpsState): CompassStar[] {
  const occ = s.tmux?.occupancy;
  if (!occ || s.tmux?.reachable !== true || occ.status === 'bad') return OCCUPANCY_COMPASS_FALLBACK_STARS;
  const stars = (occ.cells ?? []).map(paneCompassStar).filter((star): star is CompassStar => star != null);
  return stars.length ? stars : [{ dir: 'S', color: 'red' }];
}

// Signed break balance in ms → the balance dial's compact readout ('+9m'/'−12m').
const fmtBalanceValue = (ms: number): string => {
  const n = Math.round(ms / 60000);
  if (n > 0) return `+${n}m`;
  if (n < 0) return `−${Math.abs(n)}m`;
  return '0m';
};

/**
 * OpsState → the floating state-dial cluster. Live values where the contract
 * carries them (timer, balance, phone, desktop, cron, tts); honest '—'
 * placeholders for the phase-2 tail (enforce, gt, mac, wsl, mesh). Ordered
 * noteworthy-first so the important gauges fan out before the nominal tail.
 */
export function buildDials(s: OpsState): DialModel[] {
  const mode = mapMode(s.timer.mode);
  const timerTone: DialTone =
    mode === 'break' ? 'warn' : mode === 'distracted' ? 'bad' : mode === 'idle' ? 'neutral' : 'good';
  const balMs = s.timer.break_balance_ms;
  const phone = s.attention.phone;
  const cron = healthDial(s.sources.cron);
  return [
    { id: 'timer', label: 'Timer', glyph: '❚❚', value: mode.toUpperCase(), tone: timerTone, noteworthy: true,
      subtitle: 'Focus timer state — the live timer mode.', action: { kind: 'toggle-timer' } },
    { id: 'balance', label: 'Balance', glyph: '▼', value: fmtBalanceValue(balMs), tone: balMs >= 0 ? 'good' : 'bad',
      noteworthy: true, subtitle: 'Running break-balance — minutes of credit vs. debt.' },
    { id: 'phone', label: 'Phone', glyph: '✕', value: phone.app ?? 'clear', tone: phone.is_distracted ? 'bad' : 'good',
      noteworthy: true, subtitle: 'Phone foreground app — live distraction telemetry.',
      action: { kind: 'dismiss-phone' } },
    { id: 'desktop', label: 'Desktop', glyph: '▣', value: s.attention.desktop.mode || '—', tone: 'neutral',
      noteworthy: true, subtitle: 'Desktop presence — inferred from keyboard & focus.' },
    tmuxDial(s),
    fleetDial(s),
    workDial(s),
    sourceDial(s),
    enforcementDial(s),
    goldenThroneDial(s),
    // nominal / suppressed subsystems — the tail of the stack
    { id: 'cron', label: 'Cron', glyph: '◷', value: cron.value, tone: cron.tone, noteworthy: false,
      subtitle: 'Scheduled cron routines — subsystem health.' },
    ttsDial(s),
  ];
}

/**
 * OpsState → the left TTS-queue stack. Head = tts.current (speaking), then the
 * hot queue in order, then the pause queue appended behind it (same
 * TtsQueueItem shape) — all 'queued'. Persona resolves by joining the item's
 * instance_id against the active-instance roster; unknown senders fall back to
 * the generic 'astartes' key (personaIcons renders its helmet for any unmapped
 * slug rather than a hole). The short instance id stands in for the sender tag
 * — the contract carries no pane/tmux id, so none is fabricated.
 *
 * This is the ONLY mint for TTS items. The design study's
 * `workerPersonaToTtsItem` synthesizer (a fabricated utterance for a demo
 * worker finishing) is deliberately NOT ported: the TTS stack renders live
 * queue data exclusively, so a worker-born dial would be a fake entry. The
 * lifecycle weave triggers off THIS queue's arrivals/drains instead (see
 * useLifecycle in OpsCockpit.tsx).
 */
export function toTtsQueue(s: OpsState): TtsItem[] {
  const instanceOf = (instanceId: string) => s.instances.active.find((i) => i.id === instanceId);
  const shortId = (instanceId: string): string => instanceId.slice(0, 8);
  const personaOf = (item: { instance_id: string; persona_slug?: string | null }): string =>
    item.persona_slug ?? instanceOf(item.instance_id)?.persona?.slug ?? 'astartes';
  const displayNameOf = (item: { instance_id: string; name: string | null; persona_display_name?: string | null }): string =>
    item.name ?? item.persona_display_name ?? instanceOf(item.instance_id)?.display_name ?? shortId(item.instance_id);
  const commanderOf = (item: { instance_id: string; commander_type?: string | null }): string | null =>
    item.commander_type ?? instanceOf(item.instance_id)?.commander_type ?? null;

  const items: TtsItem[] = [];
  const c = s.tts.current;
  if (c) {
    items.push({
      id: `cur:${c.item_key ?? c.instance_id}:${c.started_at ?? ''}`,
      itemKey: c.item_key,
      queueState: 'current',
      promotable: false,
      text: c.message,
      route: `${c.backend ?? c.playback_target ?? 'speaking'} · ${displayNameOf(c)}`,
      senderInstanceId: c.instance_id,
      senderTmuxId: shortId(c.instance_id),
      senderName: displayNameOf(c),
      persona: personaOf(c),
      commanderType: commanderOf(c),
      playbackTarget: c.playback_target ?? null,
      status: 'speaking',
      posInQueue: 0,
    });
  }
  for (const q of [...(s.tts.hot_queue ?? []), ...(s.tts.pause_queue ?? [])]) {
    const queueState: TtsQueueState | undefined = q.queue === 'hot' || q.queue === 'pause' ? q.queue : undefined;
    items.push({
      id: q.item_key ? `tts:${q.item_key}` : `${q.queue}:${q.instance_id}:${q.queued_at}`,
      itemKey: q.item_key,
      queueState,
      promotable: Boolean(q.item_key),
      text: q.message,
      route: `${q.queue}${q.playback_target ? `/${q.playback_target}` : ''} · ${displayNameOf(q)}`,
      senderInstanceId: q.instance_id,
      senderTmuxId: shortId(q.instance_id),
      senderName: displayNameOf(q),
      persona: personaOf(q),
      commanderType: commanderOf(q),
      playbackTarget: q.playback_target ?? null,
      status: 'queued',
      posInQueue: items.length,
    });
  }
  return items;
}

// ── Lemon residents (the always-on singleton seats) ─────────────────────────
// Emperor's ruling (2026-07-09): the standing command personas live in the
// LEMON — the persona-section arc above the worker rails — not in the fleet
// queues. This set is the ONE membership definition both consumers read: the
// queue partition drops these instances (they never consume a slot) and the
// lemon activity binding lights their section while they work. Slugs are the
// registry keys; the Orchestrator seat wears the CI monogram in the lemon art
// but registers (and lights) as 'orchestrator'.
export const LEMON_RESIDENT_PERSONAS: ReadonlySet<string> = new Set([
  'custodes',
  'fabricator-general',
  'malcador',
  'pax',
  'orchestrator',
  'administratum',
]);

/**
 * OpsState → the set of lemon-resident persona slugs with a WORKING instance.
 * Drives the lemon section reverb: a slug in the set means that seat is
 * actively processing a prompt; absent means the section renders its static
 * idle glow. Subagents are excluded for the same reason the rails exclude
 * them — a child inheriting Custodes' persona must not light Custodes' seat.
 */
export function toLemonActivity(s: OpsState): Set<string> {
  const active = new Set<string>();
  for (const i of s.instances.active) {
    const slug = i.persona?.slug;
    if (!i.is_subagent && i.status === 'working' && slug && LEMON_RESIDENT_PERSONAS.has(slug)) {
      active.add(slug);
    }
  }
  return active;
}

// ── Fleet queues (two systems × two rails) ──────────────────────────────────
// The worker rails are the LIVE registration surface: one chip per registered
// instance, wearing that instance's chapter-persona icon. A chip appearing IS
// the "this instance registered properly" signal; a chip leaving means the
// instance stopped/archived. Ordered newest-registration-first: rail births
// always emerge at the centre hourglass (slot 0), so newest-first here makes
// the initial load land in the same order incremental registrations produce.
export type WorkerItem = {
  id: string; // the instance id — chip identity/React key
  persona: string; // persona slug → icon (generic 'astartes' = registered but NO persona bound)
  name: string; // instance display name — the chip's hover/aria readout
  tint: string | null; // persona chip colour; null falls back to the cycled rail tones
  chapterChild: boolean; // commander_type === 'chapter' — legitimately shares its
  //                        persona (the DB singleton trigger exempts chapter
  //                        children, so the rail's breach glow must too)
};

// One worker SYSTEM: the top (actively processing a prompt) rail + the bottom
// (idle) rail. A worker sits in exactly one of the two — the status flip
// between polls IS the inter-queue movement.
export type DomainQueues = {
  working: WorkerItem[]; // status === 'working' — the top rail
  idle: WorkerItem[]; // every other alive status — the bottom rail
};

// The two systems (Emperor's ruling, 2026-07-09): LEFT = Token-OS workers,
// RIGHT = askCivic workers. They never touch — no shared arrays, no crossover.
export type FleetQueues = {
  tokenOs: DomainQueues;
  askCivic: DomainQueues;
};

/**
 * OpsState → the four fleet rails. ONE partition pass: each active top-level
 * instance lands in exactly one of the four buckets — `domain` picks the side
 * (server-side cwd classification; the browser never sees a raw path decide),
 * `status === 'working'` picks top vs bottom. The one-queue-at-a-time
 * invariant is STRUCTURAL: a single `push` per instance, so no id can ever
 * occupy two buckets — there is nothing to filter after the fact.
 *
 * Old-payload honesty: a missing `domain` files under token-os (the home
 * fleet is the default left system), a missing `status` files as idle (never
 * fake "processing"). Subagents are excluded — the rails signal top-level
 * fleet registrations, and a subagent inheriting its parent's persona would
 * false-trigger the singleton-breach glow. Lemon-resident personas
 * (LEMON_RESIDENT_PERSONAS) are excluded too — the always-on singleton seats
 * live in the lemon's persona sections, so the rails stay mechanicus/one-off
 * territory.
 *
 * Persona falls back to the generic 'astartes' key when the instance has no
 * persona bound — the chip still appears (the registration was real) but wears
 * the generic helmet, which is exactly the "registered without a chapter
 * persona" diagnostic. Duplicate personas are NOT deduped here; the rails mark
 * them as singleton breaches (see duplicatePersonaKeys in OpsCockpit).
 */
export function toFleetQueues(s: OpsState): FleetQueues {
  // created_at crosses the boundary in BOTH SQLite ('YYYY-MM-DD HH:MM:SS') and
  // ISO ('YYYY-MM-DDTHH:MM:SS…') spellings; space sorts before 'T', so a raw
  // lexicographic compare interleaves the two formats. Normalizing the
  // separator makes the compare chronological across both.
  const regKey = (created: string | null): string => (created ?? '').replace(' ', 'T');
  const queues: FleetQueues = {
    tokenOs: { working: [], idle: [] },
    askCivic: { working: [], idle: [] },
  };
  const sorted = s.instances.active
    .filter((i) => !i.is_subagent && !LEMON_RESIDENT_PERSONAS.has(i.persona?.slug ?? ''))
    .sort((a, b) => regKey(b.created_at).localeCompare(regKey(a.created_at)));
  for (const i of sorted) {
    const system = i.domain === 'askcivic' ? queues.askCivic : queues.tokenOs;
    const bucket = i.status === 'working' ? system.working : system.idle;
    // Identity fields are IDENTICAL whichever bucket the instance lands in —
    // the chip is the same dial wherever it sits (same React key on both
    // rails, so a status flip moves the chip instead of reminting it).
    bucket.push({
      id: i.id,
      persona: i.persona?.slug ?? 'astartes',
      name: i.display_name,
      tint: i.persona?.chip_color ?? null,
      chapterChild: i.commander_type === 'chapter',
    });
  }
  return queues;
}

// ── Muster Ledger (the kanban board between the crossbars) ──────────────────
// The board renders the session-doc pipeline embedded in OpsState (the
// `session_docs` feed — ONE poller, per the #671 contract; never a bespoke
// board-side fetch). Lane membership is a PROJECTION: raw frontmatter `status:`
// (dialect-rich, Obsidian-authored) → the five canonical lifecycle lanes.
// Cards are TITLE-ONLY by Emperor's ruling (2026-07-09): the v4 ink —
// accusation line, rubric pips, raw-status stamp, live filament — over-carried
// the old card's dressing. It returns in deliberate later waves, re-cut from
// git history onto this plate.

/**
 * Raw frontmatter `status:` → canonical lane slug, per the absorption table in
 * the vault decree "Ultramar/Session Lifecycle Decree" (2026-07-09). Change
 * the decree, change this map (and the KANBAN_COLUMNS lane set with it).
 * `null` = hidden terminal — never rendered (victory-ack archives; the board
 * has no archived lane). Unknown/prose dialects project to 'astartes' (the
 * working default) with the raw stamp visible on the card. The projection
 * lives board-side until the writer-retarget PR lands canonical statuses.
 */
export function laneForStatus(status: string): string | null {
  switch (status.trim().toLowerCase()) {
    case 'stub':
    case 'ready':
    case 'planning':
    case 'dispatched':
    case 'aspirant':
      return 'aspirant';
    case 'in-review':
    case 'fix-landed-pre-merge':
    case 'parked-ready-to-merge':
    case 'arbites':
      return 'arbites';
    case 'merged':
    case 'deployment':
    case 'merged-deployed-live-verified':
    case 'inquisitor':
      return 'inquisitor';
    case 'complete':
    case 'completed':
    case 'done':
    case 'consolidated':
    case 'victorious':
      return 'victorious';
    case 'archived':
    case 'reference':
    case 'captured':
      return null; // hidden terminal — not a lane
    default:
      // active / in-progress / astartes, plus every unmapped dialect: the
      // decree's nearest-lane default is the working lane.
      return 'astartes';
  }
}

/** Local YYYY-MM-DD — the today-filter's date key, from the VIEWER's clock.
 *  The old kanban's hardcoded America/Denver is deliberately not reproduced. */
const localYmd = (now: Date): string =>
  `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;

/**
 * Today-only gate (Emperor's ruling: the Muster Ledger is a runtime-demo
 * board, not an index). Doc timestamps are local-naive ('YYYY-MM-DD HH:MM:SS'
 * or ISO-T), so a date-prefix compare IS the local-midnight test — a bare
 * `new Date('YYYY-MM-DD')` would parse as UTC midnight and misfile evening
 * docs, which is exactly the timezone landmine this avoids. Docs with no
 * usable timestamp fall back to age-since-creation vs. local midnight; a doc
 * with neither is dropped (the honesty counter still reports it).
 */
function isFromToday(doc: PipelineDoc, now: Date): boolean {
  const stamp = doc.session_date ?? doc.created_at;
  if (stamp) return String(stamp).slice(0, 10) === localYmd(now);
  if (doc.age_seconds != null) {
    const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return doc.age_seconds * 1000 <= now.getTime() - midnight.getTime();
  }
  return false;
}

const docBasename = (p: string | null): string | null => {
  if (!p) return null;
  const stem = (p.split('/').pop() ?? '').replace(/\.md$/i, '');
  return stem || null;
};

export type KanbanCardModel = {
  key: string; // stable render key — the doc id, falling back to path
  laneKey: string; // canonical lane slug (the projection of the raw status)
  title: string; // doc title, falling back to the path basename — the plate's ONLY ink
};

export type KanbanLane = {
  cards: KanbanCardModel[];
  /** Honesty counter — docs truly in this lane (per the feed's pre-cap
   *  lane_totals) beyond the cards shown. The per-lane cap and the today
   *  filter both drop docs; the board reports the drop, never hides it. */
  overflow: number;
};

/**
 * OpsState → the Muster Ledger lanes, keyed by canonical lane slug. Pure
 * projection of the embedded session_docs feed: decree lane mapping and the
 * today-only gate. `now` is injectable for tests; lanes the feed doesn't
 * populate are simply absent (an empty lane renders empty).
 */
export function toMusterBoard(s: OpsState, now: Date = new Date()): Record<string, KanbanLane> {
  const lanes: Record<string, KanbanLane> = {};
  const feed = s.session_docs;
  if (!feed) return lanes;
  const laneOf = (key: string): KanbanLane => (lanes[key] ??= { cards: [], overflow: 0 });
  for (const doc of feed.docs) {
    const laneKey = laneForStatus(doc.status);
    if (!laneKey || !isFromToday(doc, now)) continue;
    const lane = laneOf(laneKey);
    // Re-cap after projection: the feed caps per RAW status, but several raw
    // statuses absorb into one lane (active + in-progress → astartes), so the
    // raw caps can stack past the per-lane limit. Docs arrive created_at DESC,
    // so the newest survive; the honesty counter reports the rest.
    if (lane.cards.length >= feed.limit_per_lane) continue;
    lane.cards.push({
      key: doc.id != null ? `doc:${doc.id}` : `path:${doc.path ?? doc.title ?? 'unknown'}`,
      laneKey,
      title: doc.title ?? docBasename(doc.path) ?? 'untitled',
    });
  }
  // lane_totals is keyed by RAW status and counts every non-archived doc
  // (pre-cap, all days) — project each raw total onto its lane, then subtract
  // what the board actually shows.
  for (const [raw, total] of Object.entries(feed.lane_totals ?? {})) {
    const laneKey = laneForStatus(raw);
    if (laneKey) laneOf(laneKey).overflow += total;
  }
  for (const lane of Object.values(lanes)) {
    lane.overflow = Math.max(0, lane.overflow - lane.cards.length);
  }
  return lanes;
}

// ─────────────────────────────────────────────────────────────────────────
// Below-timer surfaces — OUT this phase (fleet lifecycle lands in phase 2).
// Types kept for that rebuild; there is deliberately no mock data behind them.
// ─────────────────────────────────────────────────────────────────────────
export type FleetStatus = 'processing' | 'idle' | 'stale' | 'waiting' | 'blocked';

export type FleetRow = {
  id: string;
  persona: string;
  rank: string;
  device: 'mac' | 'wsl' | 'phone';
  engine: 'claude' | 'codex';
  status: FleetStatus;
  ageLabel: string;
  zealotry: number;
  sessionDoc: string | null;
  note: string;
  talking?: boolean;
};

export type CockpitAssertion = {
  claim: string;
  value: string;
  tone: DialTone;
  confidence: 'high' | 'medium' | 'low';
  evidence: string;
};

export type CockpitEvent = { t: string; lane: string; label: string; tone: DialTone };

export type CockpitSubsystem = { label: string; value: string; detail: string; tone: DialTone };
