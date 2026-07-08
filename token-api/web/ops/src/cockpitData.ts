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

import type { OpsSourceHealth, OpsState, TimerHistory, TimerMode } from './contracts';

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

export type TtsItem = {
  id: string;
  text: string; // the utterance — surfaced in the hover tip + drawer
  route: string; // sender / delivery route (e.g. "hot · Custodes")
  senderTmuxId: string; // sender's short instance-id form (the contract carries no pane id)
  senderName: string; // sender's instance-name (the live session's descriptive name)
  persona: string; // sender's persona key → its icon (see src/personaIcons.tsx).
  //                   Lower-kebab, matching the registry keys (vault/DB slugs).
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

// a placeholder dial for subsystems NOT wired this phase — explicit, never fake.
function unwiredDial(id: string, label: string, glyph: string, what: string): DialModel {
  return {
    id, label, glyph,
    value: '—',
    tone: 'idle',
    noteworthy: false,
    subtitle: `${what} — not wired yet (phase 2).`,
  };
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
  const tts = healthDial(s.sources.tts);
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
    unwiredDial('enforce', 'Enforce', '!', 'Enforcement queue'),
    unwiredDial('gt', 'Gold. Throne', '♛', 'Golden Throne armed rubrics'),
    // nominal / suppressed subsystems — the tail of the stack
    { id: 'cron', label: 'Cron', glyph: '◷', value: cron.value, tone: cron.tone, noteworthy: false,
      subtitle: 'Scheduled cron routines — subsystem health.' },
    { id: 'tts', label: 'TTS', glyph: '♪', value: tts.value, tone: tts.tone, noteworthy: false,
      subtitle: 'Text-to-speech voice queue — subsystem health.' },
    unwiredDial('mac', 'Mac', '⌘', 'Mac node reachability'),
    unwiredDial('wsl', 'WSL', '⊞', 'WSL satellite reachability'),
    unwiredDial('mesh', 'Mesh', '⇄', 'Tailscale mesh reachability'),
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
  const personaOf = (instanceId: string): string =>
    s.instances.active.find((i) => i.id === instanceId)?.persona?.slug ?? 'astartes';
  const shortId = (instanceId: string): string => instanceId.slice(0, 8);

  const items: TtsItem[] = [];
  const c = s.tts.current;
  if (c) {
    items.push({
      id: `cur:${c.instance_id}:${c.started_at ?? ''}`,
      text: c.message,
      route: `${c.backend ?? 'speaking'} · ${c.name ?? shortId(c.instance_id)}`,
      senderTmuxId: shortId(c.instance_id),
      senderName: c.name ?? shortId(c.instance_id),
      persona: personaOf(c.instance_id),
      status: 'speaking',
      posInQueue: 0,
    });
  }
  for (const q of [...(s.tts.hot_queue ?? []), ...(s.tts.pause_queue ?? [])]) {
    items.push({
      id: `${q.queue}:${q.instance_id}:${q.queued_at}`,
      text: q.message,
      route: `${q.queue} · ${q.name ?? shortId(q.instance_id)}`,
      senderTmuxId: shortId(q.instance_id),
      senderName: q.name ?? shortId(q.instance_id),
      persona: personaOf(q.instance_id),
      status: 'queued',
      posInQueue: items.length,
    });
  }
  return items;
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
