// Centralized, typed data access. All fetching lives here so deeply-nested
// components never call endpoints ad-hoc. Each hook owns one read-model.

import { useCallback, useEffect, useRef, useState } from 'react';
import type { OpsState, TimerHistory, OpsGraph, SessionDocsFeed, TtsGlobalMode } from './types';
import { mockOpsGraph } from './mock';

export type Feed<T> = {
  data: T | null;
  error: string | null;
  /** true until the first successful (or failed) response lands */
  loading: boolean;
  /** epoch ms of the last successful fetch */
  lastOk: number | null;
  /** force an immediate refetch (resets the poll timer) */
  refresh: () => void;
};

function usesPolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
): Feed<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const controller = new AbortController();

    async function tick() {
      try {
        const payload = await fetcherRef.current(controller.signal);
        if (cancelled) return;
        setData(payload);
        setError(null);
        setLastOk(Date.now());
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) {
          setLoading(false);
          timer = window.setTimeout(tick, intervalMs);
        }
      }
    }

    tick();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer) window.clearTimeout(timer);
    };
  }, [intervalMs, refreshKey]);

  return { data, error, loading, lastOk, refresh };
}

async function getJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const res = await fetch(url, { cache: 'no-store', signal });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

/** Live cockpit state — polled fast (brief: every 2s). */
export function useOpsState(intervalMs = 2000): Feed<OpsState> {
  return usesPolling<OpsState>((signal) => getJson<OpsState>('/api/ui/ops/state', signal), intervalMs);
}

// ── Control-deck actions ──────────────────────────────────────────────────
// Thin POSTs to Token-API (the authority). These are NOT a dual-write: routing
// the mutation *through* Token-API is exactly the read-only-documents contract.

async function postJson<T = unknown>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    cache: 'no-store',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json().catch(() => ({}))) as T;
}

export type SkipResult = { skipped: boolean; cleared: number; backend?: string | null };
export type PromoteResult = { success: boolean; promoted: number };
export type GlobalModeResult = { status: string; mode: TtsGlobalMode; old_mode: string };
export type FocusResult = { snapped: boolean; reason: string | null };

/** Skip current TTS; optionally clear the whole queue. */
export function skipTts(clearQueue = false): Promise<SkipResult> {
  return postJson<SkipResult>(`/api/tts/skip?clear_queue=${clearQueue ? 'true' : 'false'}`);
}

/** Promote pause→hot: no id promotes the next item; an id promotes that instance's. */
export function promotePause(instanceId?: string): Promise<PromoteResult> {
  return postJson<PromoteResult>('/api/tts/queue/promote', instanceId ? { instance_id: instanceId } : {});
}

/** Promote all of one instance's paused items to the front of the hot queue. */
export function playPane(instanceId: string): Promise<PromoteResult> {
  return postJson<PromoteResult>('/api/tts/queue/play-pane', { instance_id: instanceId });
}

/** Set the global TTS mode (verbose | muted | silent). */
export function setGlobalMode(mode: TtsGlobalMode): Promise<GlobalModeResult> {
  return postJson<GlobalModeResult>('/api/tts/global-mode', { mode });
}

/** Human-initiated tmux focus: select + zoom the instance's pane (server-resolved). */
export function focusPane(instanceId: string): Promise<FocusResult> {
  return postJson<FocusResult>(`/api/instances/${encodeURIComponent(instanceId)}/focus-pane`);
}

export type OpenDocResult = {
  doc_id: number;
  title: string | null;
  file_path: string;
  obsidian_uri: string;
  opened: boolean;
};

/**
 * Open a session doc in Obsidian by its stable id. The one open-by-id endpoint
 * shared with the tmux `prefix + S` keybind: the server invokes the obsidian CLI
 * on the Mac. The cockpit stays read-only — this routes the open *through*
 * Token-API rather than mutating anything.
 */
export function openSessionDoc(docId: number): Promise<OpenDocResult> {
  return postJson<OpenDocResult>(`/api/session-docs/${docId}/open`);
}

export type PhoneClearResult = {
  ok: boolean;
  before: { current_app: string | null; is_distracted: boolean };
  after: { current_app: string | null; is_distracted: boolean };
  acknowledged_acks: number;
  timer_updated: boolean;
};

/** "I'm not on my phone" — force-clear phone attention when telemetry is stuck. No zap. */
export function clearPhoneAttention(): Promise<PhoneClearResult> {
  return postJson<PhoneClearResult>('/api/ui/ops/phone/clear', { source: 'ops_ui' });
}

/** Time the operator's day graph begins; the timer graph anchors here. */
const DAY_START_HOUR = 7;
const DAY_START_MINUTE = 20;

/** Seconds elapsed since today's day-start (07:20) — the graph window. */
function secondsSinceDayStart(): number {
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), DAY_START_HOUR, DAY_START_MINUTE);
  return Math.max(900, Math.floor((now.getTime() - start.getTime()) / 1000));
}

/**
 * Timer history feed. The window spans from the start of the day (07:20) to
 * now, so the graph compresses as the day fills rather than scrolling a fixed
 * window. The window is recomputed every fetch. This is live telemetry from
 * `GET /api/ui/ops/timer/history`; no mock fallback, because fake timer data is
 * worse than an explicit degraded state. Polled slowly per the brief.
 */
export function useTimerHistory(bucketSec = 60, intervalMs = 30000): Feed<TimerHistory> {
  return usesPolling<TimerHistory>(async (signal) => {
    const windowSec = secondsSinceDayStart();
    return getJson<TimerHistory>(
      `/api/ui/ops/timer/history?window=${windowSec}s&bucket=${bucketSec}s`,
      signal,
    );
  }, intervalMs);
}

/**
 * Session-doc pipeline feed. Read-only board grouped by frontmatter `status`.
 * Reads the same YAML Obsidian does; surfaces only a one-line head per doc and
 * deep-links into Obsidian for the full document. Polled slowly — pipeline
 * state changes on human timescales, not telemetry timescales.
 */
export function useSessionDocs(intervalMs = 30000): Feed<SessionDocsFeed> {
  return usesPolling<SessionDocsFeed>(
    (signal) => getJson<SessionDocsFeed>('/api/ui/ops/session-docs', signal),
    intervalMs,
  );
}

/**
 * Graph feed. Brief: do not poll large graph endpoints at the state cadence;
 * refresh on demand / slow. Falls back to mocked OpsGraph until the backend
 * read-model ships.
 */
export function useOpsGraph(graph = 'active', intervalMs = 60000): Feed<OpsGraph> {
  return usesPolling<OpsGraph>(async (signal) => {
    try {
      return await getJson<OpsGraph>(`/api/ui/ops/graph/${encodeURIComponent(graph)}`, signal);
    } catch {
      return mockOpsGraph(graph);
    }
  }, intervalMs);
}
