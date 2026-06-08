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

// Zealotry (0..3+) → intensity tone for the fervor pips.
export function zealotryTone(z: number): 'low' | 'mid' | 'high' {
  if (z >= 3) return 'high';
  if (z >= 1) return 'mid';
  return 'low';
}
