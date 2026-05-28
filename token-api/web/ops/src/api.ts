// Centralized, typed data access. All fetching lives here so deeply-nested
// components never call endpoints ad-hoc. Each hook owns one read-model.

import { useEffect, useRef, useState } from 'react';
import type { OpsState, TimerHistory, OpsGraph } from './types';
import { mockOpsGraph } from './mock';

export type Feed<T> = {
  data: T | null;
  error: string | null;
  /** true until the first successful (or failed) response lands */
  loading: boolean;
  /** epoch ms of the last successful fetch */
  lastOk: number | null;
};

function usesPolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
): Feed<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

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
  }, [intervalMs]);

  return { data, error, loading, lastOk };
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
