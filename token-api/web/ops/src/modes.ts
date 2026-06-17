// Visual language: the single source of truth for how operational states map
// to color and label. Components must read from here rather than hardcoding
// colors, so the cockpit reads as one coherent instrument.

import type { TimerMode } from './types';

export type ModeVisual = {
  label: string;
  // CSS custom-property names (defined in styles.css) so SVG + DOM agree.
  color: string; // var(--m-xxx)
  glyph: string; // short status sigil
};

const MODE_TABLE: Record<string, ModeVisual> = {
  working: { label: 'WORKING', color: 'var(--m-working)', glyph: '▲' },
  multitasking: { label: 'MULTITASK', color: 'var(--m-multi)', glyph: '◆' },
  distracted: { label: 'DISTRACTED', color: 'var(--m-distracted)', glyph: '✕' },
  break: { label: 'BREAK', color: 'var(--m-break)', glyph: '❚❚' },
  idle: { label: 'IDLE', color: 'var(--m-idle)', glyph: '·' },
  sleeping: { label: 'SLEEPING', color: 'var(--m-sleep)', glyph: '☾' },
  quiet: { label: 'QUIET', color: 'var(--m-sleep)', glyph: '○' },
  morning_session: { label: 'MORNING', color: 'var(--m-working)', glyph: '☼' },
};

const FALLBACK: ModeVisual = { label: 'UNKNOWN', color: 'var(--muted)', glyph: '?' };

export function modeVisual(mode: TimerMode | null | undefined): ModeVisual {
  if (!mode) return FALLBACK;
  return MODE_TABLE[mode.toLowerCase()] ?? { ...FALLBACK, label: mode.toUpperCase() };
}

// Instance status → tone class suffix (status--xxx in styles.css).
export function statusTone(status: string): 'good' | 'warn' | 'idle' | 'bad' | 'neutral' {
  switch (status.toLowerCase()) {
    case 'processing':
      return 'good';
    case 'idle':
      return 'idle';
    case 'stopped':
    case 'error':
      return 'bad';
    case 'waiting':
      return 'warn';
    default:
      return 'neutral';
  }
}

// Edge status → stroke styling for the node/edge graph.
export function edgeVisual(status?: string): { color: string; opacity: number } {
  switch ((status ?? 'active').toLowerCase()) {
    case 'active':
    case 'current':
      return { color: 'var(--brass-bright)', opacity: 0.95 };
    case 'stale':
      return { color: 'var(--muted)', opacity: 0.45 };
    case 'blocked':
    case 'error':
      return { color: 'var(--hazard)', opacity: 0.9 };
    case 'completed':
    case 'victory':
      return { color: 'var(--m-working)', opacity: 0.9 };
    default:
      return { color: 'var(--line-bright)', opacity: 0.6 };
  }
}

// Graph node type → accent color.
export function nodeTypeColor(type: string): string {
  switch (type.toLowerCase()) {
    case 'instance':
      return 'var(--brass)';
    case 'session_doc':
      return 'var(--cyan)';
    case 'cron_job':
      return 'var(--m-multi)';
    case 'event':
      return 'var(--muted)';
    case 'device':
      return 'var(--phosphor-dim)';
    case 'victory':
      return 'var(--m-working)';
    default:
      return 'var(--line-bright)';
  }
}

// Desktop attention → a monochrome HUD glyph (enum, not a number). Glyphs are
// geometric so they take the ring's accent color rather than rendering as
// multicolor emoji.
export function desktopGlyph(d: { mode: string; in_meeting: boolean; steam_app_name: string | null }): string {
  if (d.steam_app_name) return '◈'; // gaming
  if (d.in_meeting) return '◎'; // meeting
  return '▣'; // at the desk
}

// Phone attention → bool/enum glyph: distracted, app-open, or clear.
export function phoneGlyph(p: { app: string | null; is_distracted: boolean }): string {
  if (p.is_distracted) return '✕';
  if (p.app) return '◌';
  return '✓';
}

// Session-doc status → pipeline lane (label, accent, column order). Unknown
// statuses fall to a trailing generic lane rather than being dropped.
export type LaneVisual = { key: string; label: string; color: string; order: number };

const LANES: Record<string, LaneVisual> = {
  backlog: { key: 'backlog', label: 'Backlog', color: 'var(--muted)', order: 0 },
  active: { key: 'active', label: 'Active', color: 'var(--phosphor)', order: 1 },
  blocked: { key: 'blocked', label: 'Blocked', color: 'var(--hazard)', order: 2 },
  deployment: { key: 'deployment', label: 'Deployment', color: 'var(--cyan)', order: 3 },
  completed: { key: 'completed', label: 'Completed', color: 'var(--brass)', order: 4 },
};

export function pipelineLane(status: string | null | undefined): LaneVisual {
  const key = (status ?? 'unknown').toLowerCase();
  return LANES[key] ?? { key, label: key, color: 'var(--line-bright)', order: 9 };
}

// Zealotry (0..3+) → intensity tone for the fervor pips.
export function zealotryTone(z: number): 'low' | 'mid' | 'high' {
  if (z >= 3) return 'high';
  if (z >= 1) return 'mid';
  return 'low';
}
