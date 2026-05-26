// Mocked read-models for components whose backend endpoints are not built yet.
// Shapes are byte-for-byte the proposed contracts in the design brief, so the
// swap to live data is a one-line change in api.ts.

import type {
  TimerHistory,
  TimerHistoryPoint,
  TimerHistorySegment,
  TimerMode,
  OpsGraph,
} from './types';

// Deterministic-ish PRNG so the mock looks alive but not seizure-inducing.
function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 0xffffffff;
  };
}

const MODE_RATE_PER_MS: Record<string, number> = {
  working: 1, // earns
  multitasking: 0,
  idle: 0,
  distracted: -1, // spends
  break: -1,
  sleeping: 0,
  quiet: 0,
};

// A plausible day arc that deliberately dips into debt (negative balance)
// before recovering, so both the green-above and hazard-below rendering get
// exercised: light start, early distraction + break into debt, work back to
// credit, a second smaller dip, then a strong working recovery.
const ARC: Array<{ mode: TimerMode; minutes: number; activity: string; source?: string }> = [
  { mode: 'idle', minutes: 20, activity: 'working', source: 'startup' },
  { mode: 'distracted', minutes: 28, activity: 'distraction', source: 'reddit' },
  { mode: 'break', minutes: 32, activity: 'distraction', source: 'manual' },
  { mode: 'working', minutes: 75, activity: 'working', source: 'claude:processing' },
  { mode: 'multitasking', minutes: 25, activity: 'distraction', source: 'youtube' },
  { mode: 'distracted', minutes: 24, activity: 'distraction', source: 'instagram' },
  { mode: 'break', minutes: 26, activity: 'distraction', source: 'manual' },
  { mode: 'working', minutes: 90, activity: 'working', source: 'claude:processing' },
  { mode: 'multitasking', minutes: 16, activity: 'distraction', source: 'discord' },
  { mode: 'working', minutes: 45, activity: 'working', source: 'claude:processing' },
];

export function mockTimerHistory(windowSec = 21600, bucketSec = 60): TimerHistory {
  const now = Date.now();
  const windowMs = windowSec * 1000;
  const bucketMs = bucketSec * 1000;
  const start = now - windowMs;
  const rand = rng(0x5eed);

  // Expand the arc to fill the window, scaling segment lengths proportionally.
  const arcTotal = ARC.reduce((acc, s) => acc + s.minutes, 0) * 60 * 1000;
  const scale = windowMs / arcTotal;

  const segments: TimerHistorySegment[] = [];
  let cursor = start;
  for (const seg of ARC) {
    const len = seg.minutes * 60 * 1000 * scale;
    const segStart = cursor;
    const segEnd = Math.min(now, cursor + len);
    segments.push({
      start: new Date(segStart).toISOString(),
      end: new Date(segEnd).toISOString(),
      mode: seg.mode,
      activity: seg.activity,
      source: seg.source,
    });
    cursor = segEnd;
    if (cursor >= now) break;
  }

  function modeAt(t: number): TimerHistorySegment {
    for (const s of segments) {
      if (t >= Date.parse(s.start) && t < Date.parse(s.end)) return s;
    }
    return segments[segments.length - 1];
  }

  const points: TimerHistoryPoint[] = [];
  let balance = 8 * 60 * 1000; // start with ~8 min credit, then spend into debt
  let work = 0;
  for (let t = start; t <= now; t += bucketMs) {
    const seg = modeAt(t);
    const rate = MODE_RATE_PER_MS[seg.mode] ?? 0;
    balance += rate * bucketMs + (rate !== 0 ? (rand() - 0.5) * 1500 : 0);
    if (seg.mode === 'working') work += bucketMs;
    points.push({
      t: new Date(t).toISOString(),
      break_balance_ms: Math.round(balance),
      total_work_time_ms: Math.round(work),
      productivity_active: seg.activity === 'working' || seg.mode === 'multitasking',
      desktop_mode: seg.activity === 'working' ? 'silence' : (seg.source ?? null),
      phone_app: seg.mode === 'distracted' ? 'instagram' : null,
      mode: seg.mode,
    });
  }

  return {
    generated_at: new Date(now).toISOString(),
    window_seconds: windowSec,
    bucket_seconds: bucketSec,
    points,
    segments,
  };
}

export function mockOpsGraph(graph = 'active'): OpsGraph {
  const now = new Date().toISOString();
  return {
    graph,
    generated_at: now,
    layout_hint: 'dagre',
    nodes: [
      { id: 'cron:dawn-sweep', type: 'cron_job', label: 'dawn-sweep', subtitle: 'CronTrigger 07:00', status: 'enabled' },
      { id: 'dev:mac-mini', type: 'device', label: 'Mac-Mini', subtitle: 'primary', status: 'active' },
      { id: 'dev:wsl', type: 'device', label: 'WSL satellite', subtitle: 'TTS · enforce', status: 'active' },
      { id: 'inst:persona-assert', type: 'instance', label: 'persona-assert', subtitle: 'sisyphus · z2', status: 'processing' },
      { id: 'inst:ops-cockpit', type: 'instance', label: 'ops-cockpit', subtitle: 'terminus · z1', status: 'processing' },
      { id: 'inst:vault-canon', type: 'instance', label: 'vault-canon', subtitle: 'lexicanum · z0', status: 'idle' },
      { id: 'inst:reaper-01', type: 'instance', label: 'reaper-01', subtitle: 'subagent', status: 'stale' },
      { id: 'doc:cockpit-redesign', type: 'session_doc', label: 'cockpit redesign', subtitle: 'Terra/Sessions', status: 'active' },
      { id: 'doc:persona-loop', type: 'session_doc', label: 'persona loop fix', subtitle: 'Terra/Sessions', status: 'active' },
      { id: 'evt:victory-1', type: 'victory', label: 'victory', subtitle: 'reaper-01 sealed', status: 'completed' },
    ],
    edges: [
      { id: 'e1', source: 'cron:dawn-sweep', target: 'inst:reaper-01', type: 'spawned', directed: true, label: 'spawned', status: 'stale' },
      { id: 'e2', source: 'dev:mac-mini', target: 'inst:persona-assert', type: 'hosts', directed: true, status: 'active' },
      { id: 'e3', source: 'dev:mac-mini', target: 'inst:ops-cockpit', type: 'hosts', directed: true, status: 'active' },
      { id: 'e4', source: 'dev:mac-mini', target: 'inst:vault-canon', type: 'hosts', directed: true, status: 'active' },
      { id: 'e5', source: 'inst:ops-cockpit', target: 'doc:cockpit-redesign', type: 'bound_to', directed: true, label: 'bound', status: 'active' },
      { id: 'e6', source: 'inst:persona-assert', target: 'doc:persona-loop', type: 'bound_to', directed: true, label: 'bound', status: 'active' },
      { id: 'e7', source: 'inst:reaper-01', target: 'evt:victory-1', type: 'caused', directed: true, label: 'sealed', status: 'completed' },
      { id: 'e8', source: 'dev:wsl', target: 'dev:mac-mini', type: 'satellite_of', directed: false, status: 'active' },
      { id: 'e9', source: 'inst:persona-assert', target: 'inst:ops-cockpit', type: 'blocks', directed: true, label: 'blocks', status: 'blocked' },
    ],
  };
}
