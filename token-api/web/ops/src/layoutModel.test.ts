import { buildCockpitLayoutModel } from './layoutModel';
import type { OpsState } from './types';

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function baseState(overrides: Partial<OpsState> = {}): OpsState {
  const state: OpsState = {
    surface: 'ops',
    ui_build_id: 'test',
    generated_at: '2026-07-01T12:00:00.000Z',
    timer: {
      mode: 'working',
      activity: 'working',
      productivity_active: true,
      manual_mode: null,
      focus_active: false,
      break_balance_ms: 30 * 60_000,
      break_available_ms: 30 * 60_000,
      break_backlog_ms: 0,
      is_in_backlog: false,
      total_work_time_ms: 60 * 60_000,
      total_break_time_ms: 0,
    },
    assertions: [
      {
        id: 'timer',
        label: 'Timer',
        value: 'working',
        status: 'good',
        confidence: 'high',
        evidence: ['timer mode working'],
        freshness_seconds: 1,
        correction_hint: null,
        details: {},
      },
    ],
    attention: {
      desktop: {
        mode: 'working',
        work_mode: 'work',
        last_detection: null,
        location_zone: null,
        ahk_reachable: true,
        steam_app_name: null,
        steam_exe: null,
        in_meeting: false,
      },
      phone: {
        app: null,
        is_distracted: false,
        last_activity: null,
        app_opened_at: null,
        heartbeat_age_seconds: 10,
      },
    },
    work_state: {
      productivity_active: true,
      reason: 'test',
      active_instance_count: 2,
      processing_recent_count: 1,
      observed_agent_count: 2,
      timer_mode: 'working',
      desktop_mode: 'working',
      phone_app: null,
    },
    instances: {
      active: [],
      counts: {
        active: 2,
        stale: 0,
        by_status: { idle: 2 },
        by_engine: { codex: 2 },
        by_persona: { test: 2 },
      },
    },
    events: [],
    cron: { available: true, total_jobs: 1, enabled: 1, running: 0, runs_last_24h: 1, jobs: [] },
    tts: {
      current: null,
      routing: null,
      hot_queue: [],
      pause_queue: [],
      hot_queue_length: 0,
      pause_queue_length: 0,
      queue_length: 0,
      backend: 'mac',
      satellite_available: true,
      global_mode: 'verbose',
    },
    voice_drafts: [],
    enforcement: { available: true, pending_count: 0, pending: [], pavlok: {} },
    alarm: { acked: true, day_started_at: '2026-07-01T07:20:00.000Z', source: 'test' },
    work_actions: {
      count: 1,
      ticks: [{ at: '2026-07-01T11:59:00.000Z', source: 'test' }],
      last_at: '2026-07-01T11:59:00.000Z',
      score: 4,
      stale_fade_minutes: 30,
    },
  };
  return { ...state, ...overrides };
}

function withNested(base: OpsState, patch: Partial<OpsState>): OpsState {
  return {
    ...base,
    ...patch,
    timer: { ...base.timer, ...patch.timer },
    assertions: patch.assertions ?? base.assertions,
    attention: {
      ...base.attention,
      ...patch.attention,
      desktop: { ...base.attention.desktop, ...patch.attention?.desktop },
      phone: { ...base.attention.phone, ...patch.attention?.phone },
    },
    work_state: { ...base.work_state, ...patch.work_state },
    instances: {
      ...base.instances,
      ...patch.instances,
      counts: { ...base.instances.counts, ...patch.instances?.counts },
      active: patch.instances?.active ?? base.instances.active,
    },
    tts: { ...base.tts, ...patch.tts },
    enforcement: { ...base.enforcement, ...patch.enforcement },
    alarm: patch.alarm ?? base.alarm,
    work_actions: patch.work_actions ?? base.work_actions,
  };
}

const now = Date.parse('2026-07-01T12:00:00.000Z');

{
  const model = buildCockpitLayoutModel(baseState(), now);
  assert(model.noteworthyDials.length === 0, 'normal state should hide non-noteworthy dials');
  assert(model.hiddenDialCatalog.length >= 8, 'normal state should retain hidden dial catalog');
  assert(model.drawerSummaries.find((r) => r.side === 'right')?.count === model.hiddenDialCatalog.length, 'right rail count should match hidden catalog');
}

{
  const state = withNested(baseState(), {
    attention: { phone: { app: 'YouTube', is_distracted: true, heartbeat_age_seconds: 12 } } as OpsState['attention'],
    instances: { counts: { stale: 1 } } as OpsState['instances'],
    enforcement: { pending_count: 2 } as OpsState['enforcement'],
    tts: {
      current: {
        instance_id: 'speaker-1',
        name: 'speaker',
        message: 'speaking now',
        voice: 'Daniel',
        backend: 'mac',
        started_at: '2026-07-01T11:59:30.000Z',
      },
      queue_length: 1,
    } as OpsState['tts'],
  });
  const model = buildCockpitLayoutModel(state, now);
  const ids = model.noteworthyDials.map((dial) => dial.id);
  assert(ids.includes('phone'), 'phone distraction should be noteworthy');
  assert(ids.includes('fleet'), 'stale fleet should be noteworthy');
  assert(ids.includes('enforcement'), 'pending enforcement should be noteworthy');
  assert(ids.includes('tts'), 'current speaker should be noteworthy');
  assert(model.activeTtsWaiters.length === 1, 'current speaker should create one active TTS waiter');
}

{
  const state = withNested(baseState(), {
    tts: {
      hot_queue: [{ instance_id: 'hot-1', name: 'hot', message: 'queued hot', voice: null, queue: 'hot', queued_at: '2026-07-01T11:59:00.000Z' }],
      pause_queue: [{ instance_id: 'pause-1', name: 'pause', message: 'queued pause', voice: null, queue: 'pause', queued_at: '2026-07-01T11:58:00.000Z' }],
      hot_queue_length: 1,
      pause_queue_length: 1,
      queue_length: 2,
    } as OpsState['tts'],
  });
  const model = buildCockpitLayoutModel(state, now);
  assert(model.activeTtsWaiters.length === 2, 'queued TTS should create active waiters');
  assert(model.activeTtsWaiters[0].kind === 'hot', 'hot waiter should sort before pause waiter');
}

{
  const model = buildCockpitLayoutModel(baseState(), now);
  assert(model.activeTtsWaiters.length === 0, 'idle TTS should have no active waiters');
}
