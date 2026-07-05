import type { OpsState, StateAssertion, TtsQueueItem } from './types';
import { desktopGlyph, modeVisual, phoneGlyph } from './modes';
import { formatClock, formatSignedClock } from './format';

export type LayoutTone = 'good' | 'warn' | 'bad' | 'neutral';

export type DialId =
  | 'timer'
  | 'break'
  | 'desktop'
  | 'phone'
  | 'fleet'
  | 'enforcement'
  | 'tts'
  | 'alarm'
  | 'work'
  | 'activity';

export type DialRenderState = {
  value?: string;
  glyph?: string;
  detail?: string;
  color: string;
  ratio?: number;
  pulse?: boolean;
  title: string;
};

export type CockpitDial = {
  id: DialId;
  label: string;
  tone: LayoutTone;
  priority: number;
  reason: string;
  render: DialRenderState;
};

export type NoteworthyDial = CockpitDial & {
  noteworthy: true;
};

export type HiddenDial = CockpitDial & {
  noteworthy: false;
};

export type ActiveTtsWaiter = {
  id: string;
  kind: 'speaking' | 'hot' | 'pause';
  instanceId: string;
  label: string;
  message: string;
  voice: string | null;
  tone: LayoutTone;
  priority: number;
  queuedAt: string | null;
};

export type DrawerRailSummary = {
  side: 'left' | 'right';
  label: string;
  count: number;
  tone: LayoutTone;
  reason: string;
};

export type CockpitLayoutModel = {
  noteworthyDials: NoteworthyDial[];
  hiddenDialCatalog: HiddenDial[];
  activeTtsWaiters: ActiveTtsWaiter[];
  drawerSummaries: DrawerRailSummary[];
  orderedSections: Array<
    | 'timer-field'
    | 'active-tts'
    | 'active-fleet'
    | 'attention-evidence'
    | 'state-assertions'
    | 'session-pipeline'
    | 'event-stream'
    | 'subsystems'
    | 'relationship-graph'
  >;
  supportingAssertions: StateAssertion[];
};

const BREAK_SCALE_MS = 60 * 60 * 1000;
const WA_FRESH: [number, number, number] = [147, 217, 79];
const WA_STALE: [number, number, number] = [255, 91, 61];
const TTS_LANGUISHING_THRESHOLD = 5;
const PHONE_HEARTBEAT_STALE_SECONDS = 5 * 60;

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

function includesAny(value: string | null | undefined, needles: string[]): boolean {
  const hay = (value ?? '').toLowerCase();
  return needles.some((n) => hay.includes(n));
}

function waFadeColor(ratio: number): string {
  const mix = (a: number, b: number) => Math.round(a + (b - a) * ratio);
  return `rgb(${mix(WA_FRESH[0], WA_STALE[0])}, ${mix(WA_FRESH[1], WA_STALE[1])}, ${mix(WA_FRESH[2], WA_STALE[2])})`;
}

function waAgo(minutes: number): string {
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${Math.round(minutes)}m ago`;
  const h = Math.floor(minutes / 60);
  return `${h}h ${Math.round(minutes % 60).toString().padStart(2, '0')}m`;
}

function asNoteworthy(dial: CockpitDial, noteworthy: boolean): NoteworthyDial | HiddenDial {
  return noteworthy
    ? { ...dial, noteworthy: true }
    : { ...dial, noteworthy: false };
}

export function isAssertionNoteworthy(assertion: StateAssertion): boolean {
  return assertion.status === 'warn' || assertion.status === 'bad' || assertion.confidence === 'low';
}

export function isDesktopNoteworthy(state: OpsState): boolean {
  const desktop = state.attention.desktop;
  return Boolean(
    desktop.steam_app_name ||
      includesAny(desktop.mode, ['distract', 'scroll', 'game', 'video']) ||
      includesAny(desktop.work_mode, ['break', 'distract', 'gaming', 'scrolling']),
  );
}

export function isPhoneNoteworthy(state: OpsState): boolean {
  const phone = state.attention.phone;
  return Boolean(
    phone.is_distracted ||
      phone.app ||
      (phone.heartbeat_age_seconds != null && phone.heartbeat_age_seconds > PHONE_HEARTBEAT_STALE_SECONDS),
  );
}

export function isTtsNoteworthy(state: OpsState): boolean {
  return Boolean(
    state.tts.current ||
      state.tts.queue_length > 0 ||
      state.tts.pause_queue_length > TTS_LANGUISHING_THRESHOLD ||
      state.tts.satellite_available === false ||
      (state.tts.global_mode && state.tts.global_mode !== 'verbose'),
  );
}

function buildAllDials(state: OpsState, nowMs: number): Array<NoteworthyDial | HiddenDial> {
  const mv = modeVisual(state.timer.mode);
  const bal = state.timer.break_balance_ms;
  const debt = state.timer.is_in_backlog || bal < 0;
  const breakRatio = Math.min(1, Math.abs(bal) / BREAK_SCALE_MS);

  const desk = state.attention.desktop;
  const phone = state.attention.phone;
  const active = state.instances.counts.active;
  const stale = state.instances.counts.stale;
  const pending = state.enforcement.pending_count;

  const alarmAcked = state.alarm?.acked ?? true;
  const alarmTime = state.alarm?.day_started_at ? formatClock(state.alarm.day_started_at) : null;

  const wa = state.work_actions;
  const waFadeMin = wa?.stale_fade_minutes || 30;
  const waLastMs = wa?.last_at ? Date.parse(wa.last_at) : NaN;
  const waHasLast = Number.isFinite(waLastMs);
  const waMinsSince = waHasLast ? Math.max(0, (nowMs - waLastMs) / 60000) : null;
  const waStale = waMinsSince == null ? 0 : Math.max(0, Math.min(1, waMinsSince / waFadeMin));
  const waTone: LayoutTone = !waHasLast ? 'neutral' : waStale >= 1 ? 'bad' : waStale >= 0.5 ? 'warn' : 'good';
  const waDetail = !waHasLast ? 'none today' : waStale >= 1 ? 'log one' : waAgo(waMinsSince ?? 0);

  const timerNoteworthy = state.timer.mode !== 'working';
  const desktopNoteworthy = isDesktopNoteworthy(state);
  const phoneNoteworthy = isPhoneNoteworthy(state);
  const ttsNoteworthy = isTtsNoteworthy(state);
  const fleetNoteworthy = stale > 0 || active === 0;
  const enforcementNoteworthy = pending > 0;
  const workNoteworthy = Boolean(wa && (!waHasLast || waStale >= 1));
  const activityNoteworthy = Boolean(wa && typeof wa.score === 'number' && wa.score === 0 && state.work_state.productivity_active);

  const deskLabel = desk.steam_app_name ? truncate(desk.steam_app_name, 11) : desk.work_mode || desk.mode || '—';
  const phoneLabel = phone.app ? truncate(phone.app, 11) : 'clear';
  const ttsCount = state.tts.queue_length + (state.tts.current ? 1 : 0);

  return [
    asNoteworthy(
      {
        id: 'timer',
        label: 'Timer',
        tone: timerNoteworthy ? (state.timer.mode === 'distracted' ? 'bad' : 'warn') : 'good',
        priority: state.timer.mode === 'distracted' ? 100 : state.timer.mode === 'morning_session' ? 95 : 55,
        reason: timerNoteworthy ? `timer mode is ${state.timer.mode}` : 'expected working timer mode',
        render: {
          glyph: mv.glyph,
          detail: state.timer.mode === 'morning_session' ? 'tap to end' : mv.label.toLowerCase(),
          color: mv.color,
          title:
            state.timer.mode === 'morning_session'
              ? 'Timer mode · MORNING · tap to end morning session'
              : `Timer mode · ${mv.label}`,
        },
      },
      timerNoteworthy,
    ),
    asNoteworthy(
      {
        id: 'break',
        label: 'Break',
        tone: debt ? 'bad' : 'good',
        priority: debt ? 98 : 20,
        reason: debt ? 'break balance is in debt' : 'break balance is banked',
        render: {
          value: formatSignedClock(bal),
          detail: debt ? 'in debt' : 'banked',
          color: debt ? 'var(--hazard)' : 'var(--phosphor)',
          ratio: breakRatio,
          title: 'Break balance',
        },
      },
      debt,
    ),
    asNoteworthy(
      {
        id: 'desktop',
        label: 'Desktop',
        tone: desktopNoteworthy ? 'warn' : 'neutral',
        priority: desktopNoteworthy ? 80 : 15,
        reason: desktopNoteworthy ? 'desktop attention is not clean' : 'desktop attention is expected',
        render: {
          glyph: desktopGlyph(desk),
          detail: deskLabel,
          color: 'var(--cyan)',
          title: `Desktop · ${deskLabel}`,
        },
      },
      desktopNoteworthy,
    ),
    asNoteworthy(
      {
        id: 'phone',
        label: 'Phone',
        tone: phone.is_distracted ? 'bad' : phoneNoteworthy ? 'warn' : 'neutral',
        priority: phone.is_distracted ? 100 : phoneNoteworthy ? 70 : 10,
        reason: phone.is_distracted ? 'phone distraction is active' : phoneNoteworthy ? 'phone app/heartbeat needs review' : 'phone attention is clear',
        render: {
          glyph: phoneGlyph(phone),
          detail: phoneLabel,
          color: phone.is_distracted ? 'var(--hazard)' : 'var(--muted)',
          title: `Phone · ${phoneLabel} · tap: I'm not on my phone (2-tap clear, no zap)`,
        },
      },
      phoneNoteworthy,
    ),
    asNoteworthy(
      {
        id: 'fleet',
        label: 'Fleet',
        tone: stale > 0 ? 'warn' : active === 0 ? 'bad' : 'good',
        priority: stale > 0 ? 85 : active === 0 ? 75 : 10,
        reason: stale > 0 ? `${stale} active fleet member${stale === 1 ? '' : 's'} stale` : active === 0 ? 'no active fleet' : 'active fleet fresh',
        render: {
          value: `${active}`,
          detail: stale > 0 ? `${stale} stale` : 'all fresh',
          color: 'var(--brass)',
          ratio: active > 0 ? 1 - Math.min(1, stale / active) : undefined,
          title: 'Active fleet',
        },
      },
      fleetNoteworthy,
    ),
    asNoteworthy(
      {
        id: 'enforcement',
        label: 'Enforce',
        tone: pending > 0 ? 'bad' : 'neutral',
        priority: pending > 0 ? 96 : 10,
        reason: pending > 0 ? `${pending} enforcement acknowledgement${pending === 1 ? '' : 's'} pending` : 'no enforcement acknowledgements pending',
        render: {
          value: `${pending}`,
          detail: `tts q ${state.tts.queue_length}`,
          color: pending > 0 ? 'var(--hazard)' : 'var(--muted)',
          ratio: pending > 0 ? 1 : undefined,
          pulse: pending > 0,
          title: 'Enforcement pending',
        },
      },
      enforcementNoteworthy,
    ),
    asNoteworthy(
      {
        id: 'tts',
        label: 'TTS',
        tone: state.tts.satellite_available === false ? 'bad' : ttsCount > 0 ? 'warn' : 'neutral',
        priority: state.tts.satellite_available === false ? 90 : ttsCount > 0 ? 72 : 5,
        reason: state.tts.current ? 'TTS is speaking' : state.tts.queue_length > 0 ? 'TTS queue has waiters' : 'TTS is idle',
        render: {
          value: `${ttsCount}`,
          detail: state.tts.current ? 'speaking' : state.tts.queue_length > 0 ? 'queued' : state.tts.global_mode ?? 'idle',
          color: state.tts.satellite_available === false ? 'var(--hazard)' : ttsCount > 0 ? 'var(--brass)' : 'var(--muted)',
          ratio: ttsCount > 0 ? Math.min(1, ttsCount / 6) : undefined,
          pulse: Boolean(state.tts.current),
          title: 'TTS active waiters',
        },
      },
      ttsNoteworthy,
    ),
    asNoteworthy(
      {
        id: 'alarm',
        label: 'Alarm',
        tone: alarmAcked ? 'good' : 'warn',
        priority: alarmAcked ? 5 : 65,
        reason: alarmAcked ? 'alarm acknowledged' : 'alarm acknowledgement pending',
        render: {
          glyph: alarmAcked ? '✓' : '○',
          detail: alarmAcked ? alarmTime ?? 'acked' : 'pending',
          color: alarmAcked ? 'var(--phosphor)' : 'var(--muted)',
          title: 'Alarm acknowledgement',
        },
      },
      !alarmAcked,
    ),
    ...(wa
      ? [
          asNoteworthy(
            {
              id: 'work',
              label: 'Work',
              tone: waTone,
              priority: waTone === 'bad' ? 76 : waTone === 'warn' ? 50 : 5,
              reason: !waHasLast ? 'no explicit work action today' : waStale >= 1 ? 'work action is stale' : 'work action is fresh',
              render: {
                value: `${wa.count}`,
                detail: waDetail,
                color: waHasLast ? waFadeColor(waStale) : 'var(--muted)',
                ratio: waHasLast ? waStale : undefined,
                pulse: waStale >= 1,
                title: waHasLast
                  ? `${wa.count} work action${wa.count === 1 ? '' : 's'} today · last ${formatClock(wa.last_at)}`
                  : 'No work actions logged today',
              },
            },
            workNoteworthy,
          ),
          asNoteworthy(
            {
              id: 'activity',
              label: 'Activity',
              tone: activityNoteworthy ? 'warn' : 'neutral',
              priority: activityNoteworthy ? 45 : 1,
              reason: activityNoteworthy ? 'productivity active with no aggregate work signals' : 'aggregate work signals normal',
              render: {
                value: `${wa.score}`,
                detail: 'all signals',
                color: 'var(--brass)',
                title: 'Aggregate work signals today (non-load-bearing)',
              },
            },
            activityNoteworthy,
          ),
        ]
      : []),
  ];
}

function waiterFromQueue(item: TtsQueueItem, kind: 'hot' | 'pause', index: number): ActiveTtsWaiter {
  return {
    id: `${kind}-${item.instance_id}-${item.queued_at}-${index}`,
    kind,
    instanceId: item.instance_id,
    label: item.name || item.instance_id.slice(0, 8),
    message: item.message,
    voice: item.voice,
    tone: kind === 'hot' ? 'warn' : 'neutral',
    priority: kind === 'hot' ? 70 - index : 40 - index,
    queuedAt: item.queued_at,
  };
}

export function activeTtsWaiters(state: OpsState): ActiveTtsWaiter[] {
  const waiters: ActiveTtsWaiter[] = [];
  if (state.tts.current) {
    waiters.push({
      id: `speaking-${state.tts.current.instance_id}`,
      kind: 'speaking',
      instanceId: state.tts.current.instance_id,
      label: state.tts.current.name || state.tts.current.instance_id.slice(0, 8),
      message: state.tts.current.message,
      voice: state.tts.current.voice,
      tone: 'good',
      priority: 100,
      queuedAt: state.tts.current.started_at ?? null,
    });
  }
  state.tts.hot_queue.forEach((item, index) => waiters.push(waiterFromQueue(item, 'hot', index)));
  state.tts.pause_queue.forEach((item, index) => waiters.push(waiterFromQueue(item, 'pause', index)));
  return waiters.sort((a, b) => b.priority - a.priority);
}

export function buildCockpitLayoutModel(state: OpsState, nowMs = Date.now()): CockpitLayoutModel {
  const dials = buildAllDials(state, nowMs);
  const noteworthyDials = dials
    .filter((dial): dial is NoteworthyDial => dial.noteworthy)
    .sort((a, b) => b.priority - a.priority);
  const hiddenDialCatalog = dials
    .filter((dial): dial is HiddenDial => !dial.noteworthy)
    .sort((a, b) => b.priority - a.priority);
  const waiters = activeTtsWaiters(state);
  const supportingAssertions = (state.assertions ?? []).filter(isAssertionNoteworthy);
  const leftCount = waiters.length + (state.voice_drafts?.length ?? 0);

  return {
    noteworthyDials,
    hiddenDialCatalog,
    activeTtsWaiters: waiters,
    drawerSummaries: [
      {
        side: 'left',
        label: 'TTS / locks',
        count: leftCount,
        tone: state.tts.satellite_available === false ? 'bad' : leftCount > 0 ? 'warn' : 'neutral',
        reason: leftCount > 0 ? 'active voice waiters hidden behind left rail' : 'voice drawer idle',
      },
      {
        side: 'right',
        label: 'Dial catalog',
        count: hiddenDialCatalog.length,
        tone: noteworthyDials.some((d) => d.tone === 'bad') ? 'bad' : noteworthyDials.length > 0 ? 'warn' : 'neutral',
        reason: `${hiddenDialCatalog.length} expected dial${hiddenDialCatalog.length === 1 ? '' : 's'} suppressed from HUD`,
      },
    ],
    orderedSections: [
      'timer-field',
      'active-tts',
      'active-fleet',
      'attention-evidence',
      'state-assertions',
      'session-pipeline',
      'event-stream',
      'subsystems',
      'relationship-graph',
    ],
    supportingAssertions,
  };
}
