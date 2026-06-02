// Bespoke time-series chart. No chart library: a few hundred lines of SVG buy
// full control over the segmented mode shading and threshold-colored balance
// line the brief asks for, and keep the committed build dependency-free.
//
// Consumes the `TimerHistory` contract directly.

import { useEffect, useMemo, useRef, useState } from 'react';
import type { TimerHistory } from '../types';
import { modeVisual } from '../modes';
import { formatSignedDuration, formatClock } from '../format';

const PAD = { top: 16, right: 16, bottom: 26, left: 52 };
const HEIGHT = 240;

// Curated allowlist: only work-session boundaries and daily/manual resets
// render as full-height dividers. Deliberately no mode_change or enforcement
// clutter — work sessions + resets are the only structural marks worth a line.
const DIVIDER_TYPES: Record<string, { glyph: string; sev: 'good' | 'bad' | 'info' }> = {
  work_session_start: { glyph: 'WS▶', sev: 'good' },
  work_session_end: { glyph: 'WS■', sev: 'good' },
  work_session_cancel: { glyph: 'WS✕', sev: 'bad' },
  daily_reset: { glyph: 'RESET', sev: 'info' },
  manual_reset: { glyph: 'RESET', sev: 'info' },
};

type Props = { history: TimerHistory };

export function TimerGraph({ history }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(900);
  const [hover, setHover] = useState<number | null>(null);

  // Observe container width for responsive sizing.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) setWidth(Math.max(320, Math.floor(w)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const { points, segments, gaps = [] } = history;
  const anomalyCount = history.anomaly_summary?.count ?? history.anomalies?.length ?? 0;
  const gapCount = history.anomaly_summary?.gap_count ?? gaps.length;
  // A wall of anomalies is a reverse signal — the backend collapses it to a
  // suspect-detection record. Render it as a calm diagnostic, not a red alarm.
  const bulkSuspected = history.anomaly_summary?.bulk_suspected ?? false;
  const suppressedCount = history.anomaly_summary?.suppressed_count ?? 0;

  const geom = useMemo(() => {
    if (points.length === 0) return null;
    const generatedAt = Date.parse(history.generated_at);
    const windowStart = generatedAt - history.window_seconds * 1000;
    const firstPoint = Date.parse(points[0].t);
    const lastPoint = Date.parse(points[points.length - 1].t);
    const t0 = Number.isFinite(windowStart) ? Math.min(windowStart, firstPoint) : firstPoint;
    const t1 = Number.isFinite(generatedAt) ? Math.max(generatedAt, lastPoint) : lastPoint;
    const span = Math.max(1, t1 - t0);

    const balances = points.map((p) => p.break_balance_ms);
    const loRaw = Math.min(0, ...balances);
    const hiRaw = Math.max(0, ...balances);

    // Quantize the axis to quarter-hour steps. Pick the smallest step from a
    // ladder of 15-minute multiples that keeps the gridline count sane — never
    // a blind divide, and every step stays on a clean %15 value. Then snap the
    // domain to that step so top/bottom/zero all land on quarter-hour values.
    const MIN = 60_000;
    const rangeMin = Math.max(15, (hiRaw - loRaw) / MIN);
    const stepMs = chooseQuarterHourStep(rangeMin) * MIN;
    let lo = Math.floor(loRaw / stepMs) * stepMs;
    let hi = Math.ceil(hiRaw / stepMs) * stepMs;
    if (hi === lo) hi += stepMs;

    const plotW = width - PAD.left - PAD.right;
    const plotH = HEIGHT - PAD.top - PAD.bottom;
    const x = (t: number) => PAD.left + ((t - t0) / span) * plotW;
    const y = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * plotH;

    return { t0, t1, span, lo, hi, stepMs, plotW, plotH, x, y };
  }, [history.generated_at, history.window_seconds, points, width]);

  // Curated work-session/reset dividers, derived from the annotations the
  // backend builds off timer_shifts. Keep only allowlisted types that land in
  // the window with a finite timestamp; suppress a glyph label when it would
  // crowd the previously-labeled divider (~34px).
  const dividers = useMemo(() => {
    if (!geom) return [];
    const { x, t0, span } = geom;
    const t1 = t0 + span;
    const kept = (history.annotations ?? [])
      .map((a) => {
        const spec = DIVIDER_TYPES[a.type];
        const t = Date.parse(a.t);
        if (!spec || !Number.isFinite(t) || t < t0 || t > t1) return null;
        return { a, px: x(t), glyph: spec.glyph, sev: spec.sev };
      })
      .filter((d): d is NonNullable<typeof d> => d !== null)
      .sort((p, q) => p.px - q.px);
    let lastLabeledPx = -Infinity;
    return kept.map((d) => {
      const showLabel = d.px - lastLabeledPx >= 34;
      if (showLabel) lastLabeledPx = d.px;
      return { ...d, showLabel };
    });
  }, [geom, history.annotations]);

  if (!geom) {
    return (
      <div className="chart-empty">No timer history in window.</div>
    );
  }

  const { t0, span, lo, hi, stepMs, plotW, plotH, x, y } = geom;
  const zeroY = y(0);

  // Balance paths are split on explicit sample gaps. A restart/data gap must
  // not draw a straight line between the last pre-restart sample and the first
  // recovered/live point.
  const runs = splitPointRuns(points);
  const markerPoints = points.filter(
    (point) => point.anomaly || point.gap_before || point.sample_source === 'timer_shift',
  );
  const linePaths = runs
    .map((run) => ({
      points: run,
      d: run
        .map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(Date.parse(p.t)).toFixed(1)} ${y(p.break_balance_ms).toFixed(1)}`)
        .join(' '),
    }))
    .filter((run) => run.d.length > 0);

  const yTicks = quarterHourTicks(lo, hi, stepMs);
  const tape = hourTapeTicks(t0, t0 + span);

  const hoverPoint = hover != null ? points[hover] : null;
  const hoverX = hoverPoint ? x(Date.parse(hoverPoint.t)) : 0;

  function onMove(e: React.MouseEvent<SVGRectElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - rect.left + PAD.left;
    const ratio = (px - PAD.left) / plotW;
    const idx = Math.round(ratio * (points.length - 1));
    setHover(Math.max(0, Math.min(points.length - 1, idx)));
  }

  return (
    <div className="chart" ref={wrapRef}>
      <svg width={width} height={HEIGHT} role="img" aria-label="Break balance over time">
        <defs>
          <clipPath id="tg-above"><rect x={PAD.left} y={PAD.top} width={plotW} height={Math.max(0, zeroY - PAD.top)} /></clipPath>
          <clipPath id="tg-below"><rect x={PAD.left} y={zeroY} width={plotW} height={Math.max(0, HEIGHT - PAD.bottom - zeroY)} /></clipPath>
          <linearGradient id="tg-fill-pos" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--m-working)" stopOpacity="0.28" />
            <stop offset="100%" stopColor="var(--m-working)" stopOpacity="0.02" />
          </linearGradient>
          <linearGradient id="tg-fill-neg" x1="0" y1="1" x2="0" y2="0">
            <stop offset="0%" stopColor="var(--hazard)" stopOpacity="0.30" />
            <stop offset="100%" stopColor="var(--hazard)" stopOpacity="0.02" />
          </linearGradient>
        </defs>

        {/* Mode shading bands */}
        {segments.map((seg, i) => {
          const sx = x(Math.max(t0, Date.parse(seg.start)));
          const ex = x(Math.min(t0 + span, Date.parse(seg.end)));
          const w = Math.max(0, ex - sx);
          if (w <= 0) return null;
          const v = modeVisual(seg.mode);
          return (
            <g key={i} aria-label={v.label}>
              <rect x={sx} y={PAD.top} width={w} height={plotH} fill={v.color} opacity={0.1} />
            </g>
          );
        })}

        {/* Explicit telemetry gaps */}
        {gaps.map((gap, i) => {
          const sx = x(Math.max(t0, Date.parse(gap.start)));
          const ex = x(Math.min(t0 + span, Date.parse(gap.end)));
          const w = Math.max(0, ex - sx);
          if (w <= 0) return null;
          const labelX = sx + Math.min(Math.max(w / 2, 18), Math.max(18, w - 18));
          return (
            <g key={`gap-${i}`} aria-label={`telemetry gap: ${gap.reason}`}>
              <rect x={sx} y={PAD.top} width={w} height={plotH} fill="var(--hazard)" opacity={0.07} />
              <line x1={sx} x2={sx} y1={PAD.top} y2={HEIGHT - PAD.bottom} stroke="var(--hazard)" strokeOpacity={0.35} strokeDasharray="3 4" />
              <line x1={ex} x2={ex} y1={PAD.top} y2={HEIGHT - PAD.bottom} stroke="var(--hazard)" strokeOpacity={0.25} strokeDasharray="3 4" />
              {w > 42 ? (
                <text x={labelX} y={PAD.top + 14} className="gap-label" textAnchor="middle">
                  {gap.anomaly_reason ?? gap.reason}
                </text>
              ) : null}
            </g>
          );
        })}

        {/* Y gridlines + labels */}
        {yTicks.map((v) => (
          <g key={v}>
            <line x1={PAD.left} x2={width - PAD.right} y1={y(v)} y2={y(v)} className="grid" />
            <text x={PAD.left - 8} y={y(v) + 3} className="axis-y" textAnchor="end">
              {formatSignedDuration(v)}
            </text>
          </g>
        ))}

        {/* Filled areas split at zero */}
        {linePaths.map((run, i) => {
          const first = run.points[0];
          const last = run.points[run.points.length - 1];
          if (!first || !last) return null;
          const startX = x(Date.parse(first.t)).toFixed(1);
          const endX = x(Date.parse(last.t)).toFixed(1);
          const areaPath = `${run.d} L ${endX} ${zeroY} L ${startX} ${zeroY} Z`;
          return (
            <g key={`area-${i}`}>
              <path d={areaPath} fill="url(#tg-fill-pos)" clipPath="url(#tg-above)" />
              <path d={areaPath} fill="url(#tg-fill-neg)" clipPath="url(#tg-below)" />
            </g>
          );
        })}

        {/* Zero line — prominent */}
        <line x1={PAD.left} x2={width - PAD.right} y1={zeroY} y2={zeroY} className="zero-line" />

        {/* Work-session + reset dividers — over shading/grid, under the data line */}
        <g className="dividers">
          {dividers.map((d) => (
            <g key={d.a.id} className={`divider divider--${d.sev}`}>
              <line x1={d.px} x2={d.px} y1={PAD.top} y2={HEIGHT - PAD.bottom} className="divider__line" />
              {d.showLabel ? (
                <text x={d.px} y={PAD.top + 9} className="divider__label" textAnchor="middle">{d.glyph}</text>
              ) : null}
              <title>{`${formatClock(d.a.t)} · ${d.a.type} · ${d.a.label}`}</title>
            </g>
          ))}
        </g>

        {/* Balance line, threshold colored */}
        {linePaths.map((run, i) => (
          <g key={`line-${i}`}>
            <path d={run.d} className="bal-line bal-line--pos" clipPath="url(#tg-above)" />
            <path d={run.d} className="bal-line bal-line--neg" clipPath="url(#tg-below)" />
          </g>
        ))}

        {/* Only mark gaps/anomalies/sparse fallback points. Normal samples should read as a line. */}
        {markerPoints.map((point, i) => (
          <circle
            key={`pt-${i}`}
            cx={x(Date.parse(point.t))}
            cy={y(point.break_balance_ms)}
            r={point.anomaly ? 4.2 : point.gap_before ? 3.4 : 2.6}
            className={`sample-dot${point.gap_before ? ' sample-dot--gap' : ''}${point.anomaly ? ' sample-dot--anomaly' : ''}`}
          />
        ))}

        {/* X axis — tape-measure: labeled hour marks, medium :30, minor :15/:45 */}
        {tape.map((tk) => {
          const tx = x(tk.t);
          const len = tk.kind === 'major' ? 9 : tk.kind === 'mid' ? 6 : 4;
          const axisY = HEIGHT - PAD.bottom;
          return (
            <g key={tk.t}>
              <line x1={tx} x2={tx} y1={axisY} y2={axisY + len} className={`tick tick--${tk.kind}`} />
              {tk.kind === 'major' ? (
                <text x={tx} y={HEIGHT - 7} className="axis-x" textAnchor="middle">
                  {tk.label}
                </text>
              ) : null}
            </g>
          );
        })}

        {/* Hover crosshair */}
        {hoverPoint ? (
          <g>
            <line x1={hoverX} x2={hoverX} y1={PAD.top} y2={HEIGHT - PAD.bottom} className="crosshair" />
            <circle cx={hoverX} cy={y(hoverPoint.break_balance_ms)} r={4} className="cursor-dot" />
          </g>
        ) : null}

        {/* Interaction surface */}
        <rect
          x={PAD.left}
          y={PAD.top}
          width={plotW}
          height={plotH}
          fill="transparent"
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
        />
      </svg>

      {bulkSuspected ? (
        <div className="chart-warning chart-warning--suspect">
          telemetry suspect · {suppressedCount} anomalies in one batch reads as
          false detection, not rendered
          {gapCount > 0 ? ` · ${gapCount} telemetry gap${gapCount === 1 ? '' : 's'}` : ''}
        </div>
      ) : gapCount > 0 || anomalyCount > 0 ? (
        <div className={`chart-warning${anomalyCount > 0 ? ' chart-warning--bad' : ''}`}>
          {anomalyCount > 0
            ? `${anomalyCount} timer anomaly${anomalyCount === 1 ? '' : 'ies'}`
            : `${gapCount} telemetry gap${gapCount === 1 ? '' : 's'}`}
          {gapCount > 0 ? ` · graph line split at missing telemetry` : ''}
        </div>
      ) : null}

      {hoverPoint ? (
        <div
          className="chart-tip"
          style={{ left: `${Math.min(hoverX, width - 180)}px` }}
        >
          <div className="chart-tip__time">{formatClock(hoverPoint.t)}</div>
          <div className="chart-tip__row">
            <span className="dot-sm" style={{ background: modeVisual(hoverPoint.mode).color }} />
            <strong>{modeVisual(hoverPoint.mode).label}</strong>
          </div>
          <div className="chart-tip__row">balance <strong>{formatSignedDuration(hoverPoint.break_balance_ms)}</strong></div>
          <div className="chart-tip__row chart-tip__muted">
            {hoverPoint.productivity_active ? 'productive' : 'inactive'}
            {hoverPoint.desktop_mode ? ` · ${hoverPoint.desktop_mode}` : ''}
            {hoverPoint.phone_app ? ` · 📱${hoverPoint.phone_app}` : ''}
          </div>
          {hoverPoint.gap_before ? (
            <div className="chart-tip__row chart-tip__warn">
              gap: {hoverPoint.anomaly_reason ?? hoverPoint.gap_reason ?? 'telemetry'}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// ── tick helpers ──────────────────────────────────────────────────────────

function splitPointRuns(points: TimerHistory['points']) {
  const runs: TimerHistory['points'][] = [];
  for (const point of points) {
    if (runs.length === 0 || point.gap_before) {
      runs.push([point]);
    } else {
      runs[runs.length - 1].push(point);
    }
  }
  return runs;
}

// Ladder of quarter-hour multiples (minutes). The Y step is whichever rung
// keeps the gridline count reasonable — chosen, not computed by division, so
// every value stays on a clean %15 boundary.
const STEP_LADDER_MIN = [15, 30, 45, 60, 90, 120, 180, 240, 360, 480, 720];
const MAX_GRIDLINES = 6;

function chooseQuarterHourStep(rangeMin: number): number {
  for (const step of STEP_LADDER_MIN) {
    if (rangeMin / step <= MAX_GRIDLINES) return step;
  }
  // Beyond the ladder, round up to a whole hour that fits.
  return Math.ceil(rangeMin / MAX_GRIDLINES / 60) * 60;
}

// Ticks at every quarter-hour step across the (already-snapped) domain. Both
// bounds and the step are multiples of `stepMs`, so zero falls on a tick.
function quarterHourTicks(lo: number, hi: number, stepMs: number): number[] {
  const ticks: number[] = [];
  for (let v = lo; v <= hi + 1; v += stepMs) ticks.push(Math.round(v));
  return ticks;
}

type TapeTick = { t: number; kind: 'major' | 'mid' | 'minor'; label: string };

// Tape-measure X axis: a mark every 15 min. Labeled on the hour, a medium
// mark at :30, small marks at :15 and :45.
function hourTapeTicks(t0: number, t1: number): TapeTick[] {
  const QUARTER = 15 * 60_000;
  const first = Math.ceil(t0 / QUARTER) * QUARTER;
  const ticks: TapeTick[] = [];
  for (let t = first; t <= t1; t += QUARTER) {
    const d = new Date(t);
    const m = d.getMinutes();
    const kind: TapeTick['kind'] = m === 0 ? 'major' : m === 30 ? 'mid' : 'minor';
    const label = m === 0 ? `${d.getHours().toString().padStart(2, '0')}:00` : '';
    ticks.push({ t, kind, label });
  }
  return ticks;
}
